"""temp_zone · 解冻转层：批级层位覆盖。

结构选型：批级层位覆盖（lots.layer_override，NULL = 跟随品项默认层），
而不是"拆出品项变体"（为中层冻饺另建 item）。理由：

- 一个 lot 任意时刻只有一个有效层位 —— 覆盖值是行内单列，从结构上
  禁止"下层和中层同时挂同一个 lot id"；变体方案要靠跨表不变量维持。
- item_id 不变，FEFO 消费仍按品项跨层合并扣减；变体方案会把"冻饺"
  劈成两个品项，消费时中层的批扣不到，等于只改了过滤。
- 有效层位是标量表达式 EFFECTIVE_LAYER，层页 / 顶条 / 下架 / 消费
  全部读同一处，不存在第二份真相。

并发：transfer_lot 先读当前行做精确失败归因，再用一条带全部资格谓词
的条件 UPDATE（CAS）落写；调用方必须处于 db.tx()（BEGIN IMMEDIATE）
事务中，与消费、过期下架串行化。过期资格与下架名单、顶条 expired
共用 app.engines.fefo.is_expired，三套名单永远一致。
"""
from app.engines.fefo import is_expired

LAYERS = ("upper", "mid", "lower")

# 有效层位 SQL 片段（lots JOIN items 上下文中使用）
EFFECTIVE_LAYER = "COALESCE(lots.layer_override, items.layer)"

# 同上，但以 lots 单表为上下文（条件 UPDATE 的 WHERE 里用相关子查询取品项层）
_EFFECTIVE_LAYER_SELF = (
    "COALESCE(layer_override, (SELECT items.layer FROM items WHERE items.id = lots.item_id))"
)

def transfer_lot(c, lot_id: int, to_layer: str, today: str, from_layer: str | None = None) -> dict:
    """把单个批转到目标层。成功返回 {"ok": True, lot_id, from_layer, to_layer}。

    资格（与下架、顶条同一套判定）：在架 on_shelf、余量 > 0、未过期；
    传了 from_layer 时要求当前有效层位与之相等（CAS，防并发下重复转）。
    任何失败都不落任何写；调用方事务回滚即整体回到转前。
    """
    if to_layer not in LAYERS:
        return {"ok": False, "reason": "bad_layer", "layers": list(LAYERS)}
    row = c.execute(
        f"SELECT lots.*, {EFFECTIVE_LAYER} AS eff_layer FROM lots "
        "JOIN items ON items.id = lots.item_id WHERE lots.id = ?",
        (lot_id,)).fetchone()
    if row is None:
        return {"ok": False, "reason": "not_found"}
    if row["status"] != "on_shelf":
        return {"ok": False, "reason": "not_on_shelf", "status": row["status"]}
    if float(row["qty_remain"] or 0) <= 0:
        return {"ok": False, "reason": "no_qty"}
    if is_expired(row["expiry"], today):
        return {"ok": False, "reason": "expired", "expiry": row["expiry"]}
    cur_layer = row["eff_layer"]
    if cur_layer == to_layer:
        return {"ok": False, "reason": "same_layer", "layer": cur_layer}
    if from_layer is not None and from_layer != cur_layer:
        return {"ok": False, "reason": "layer_mismatch", "expected": from_layer, "actual": cur_layer}
    # 单条条件 UPDATE：资格谓词随写一起原子生效。BEGIN IMMEDIATE 下
    # 读-写之间不会有第三者，rowcount != 1 只是兜底（比如未来有人把
    # 事务降级成 deferred），命中时调用方回滚，不留半完成状态。
    cur = c.execute(
        f"UPDATE lots SET layer_override = ? WHERE id = ? "
        f"AND status = 'on_shelf' AND qty_remain > 0 "
        f"AND (expiry IS NULL OR expiry > ?) AND {_EFFECTIVE_LAYER_SELF} = ?",
        (to_layer, lot_id, today, cur_layer))
    if cur.rowcount != 1:
        return {"ok": False, "reason": "conflict"}
    return {"ok": True, "lot_id": lot_id, "from_layer": cur_layer, "to_layer": to_layer}
