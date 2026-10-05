"""解冻转层 (temp_zone)：把单个批次从一层转到另一层（如下层冷冻 → 中层解冻）。

结构选择：批级层位覆盖（lots.layer_override，NULL = 跟随品项默认层），
而不是拆出品项变体（为中层另建 item 并把 lot 改挂过去）。理由：

- 单个 lot 任意时刻只有一个有效层 COALESCE(layer_override, items.layer)，
  「同一 lot id 同时挂在下层和中层」在结构上不可能出现，无需事后校验。
- 品项目录不膨胀，FEFO 消费、顶条预警、过期下架仍按同一 item 聚合；
  变体方案会把同品项的 FEFO 队列劈成两条，并污染 alerts/报表的分组。
- 转层 = 一次 UPDATE + 一条 transfers 审计，落在与消费/下架相同的
  write_txn（BEGIN IMMEDIATE）里：三者互相串行，读-判-写不会被并发插入。
  任一步失败整体回滚——层位、余量、以及由 lots 派生的顶条紧急集合同进同退，
  不存在"层改了但余量/预警没跟上"的中间态。

过期资格与下架名单、顶条 expired 共用 engines.fefo.is_expired 这同一个谓词：
已过期（即便还 on_shelf、下架扫描尚未跑到）的批次一律拒绝转层，
只能走过期下架，保证顶条、层页、下架名单看到的是同一套。
"""
from datetime import date, datetime, timezone

from app.db import write_txn
from app.engines.fefo import is_expired

LAYERS = ("upper", "mid", "lower")

# 有效层：批级覆盖优先，缺省回落到品项层。所有按层查询（层页/全层/按层消费）
# 都必须用这一个表达式，禁止各自另写过滤条件。
EFFECTIVE_LAYER = "COALESCE(lots.layer_override, items.layer)"


def ensure_schema(c):
    """建/迁本模块拥有的结构：lots.layer_override 列 + transfers 审计表。"""
    cols = [r["name"] for r in c.execute("PRAGMA table_info(lots)")]
    if "layer_override" not in cols:
        c.execute("ALTER TABLE lots ADD COLUMN layer_override TEXT")
    c.execute("""
      CREATE TABLE IF NOT EXISTS transfers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lot_id INT NOT NULL, from_layer TEXT NOT NULL, to_layer TEXT NOT NULL,
        created_at TEXT NOT NULL
      )""")


class _Reject(Exception):
    """业务拒绝：回滚事务并以 {ok: False, reason} 返回，不抛 500。"""

    def __init__(self, reason: str, **extra):
        super().__init__(reason)
        self.reason, self.extra = reason, extra


def transfer_lot(c, lot_id: int, to_layer: str, from_layer: str | None = None,
                 today: str | None = None) -> dict:
    """把单个 lot 转到 to_layer。只动这一批，同品项其余批次不受影响。

    from_layer 为可选的乐观并发守卫：调用方（前端下层页）声明它看到的当前层，
    若服务端有效层已变（并发的转层/下架/消费抢先），拒绝并返回 layer_changed，
    由调用方重载后再决定。整个检查+更新在一个 write_txn 内完成。
    """
    today = today or date.today().isoformat()
    if to_layer not in LAYERS:
        return {"ok": False, "reason": "bad_layer", "layers": list(LAYERS)}
    try:
        with write_txn(c):
            row = c.execute(
                f"""SELECT lots.id, lots.status, lots.qty_remain, lots.expiry,
                           {EFFECTIVE_LAYER} AS eff_layer
                    FROM lots JOIN items ON items.id = lots.item_id
                    WHERE lots.id = ?""", (lot_id,)).fetchone()
            if row is None:
                raise _Reject("lot_not_found")
            if row["status"] != "on_shelf":
                raise _Reject("not_on_shelf", status=row["status"])
            if float(row["qty_remain"]) <= 0:
                raise _Reject("empty")
            if is_expired(row["expiry"], today):
                # 与 expire-sweep / 顶条 expired 同一套：该批只能下架，不能转层
                raise _Reject("expired", expiry=row["expiry"])
            eff = row["eff_layer"]
            if from_layer is not None and from_layer != eff:
                raise _Reject("layer_changed", layer=eff)
            if to_layer == eff:
                raise _Reject("same_layer", layer=eff)
            cur = c.execute(
                "UPDATE lots SET layer_override=? "
                "WHERE id=? AND status='on_shelf' AND qty_remain>0",
                (to_layer, lot_id))
            if cur.rowcount != 1:
                raise _Reject("conflict")
            c.execute(
                "INSERT INTO transfers(lot_id,from_layer,to_layer,created_at) VALUES (?,?,?,?)",
                (lot_id, eff, to_layer, datetime.now(timezone.utc).isoformat()))
    except _Reject as r:
        return {"ok": False, "reason": r.reason, **r.extra}
    return {"ok": True, "lot_id": lot_id, "from_layer": eff, "to_layer": to_layer}
