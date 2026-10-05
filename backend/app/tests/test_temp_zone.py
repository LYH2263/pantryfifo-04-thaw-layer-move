"""解冻转层：批级层位覆盖的语义与并发测试。

每个用例用独立临时库（DATA_DIR 指向 tmp_path），seed 后自建品项/批次，
不依赖种子数据里的固定日期。
"""
import threading
from datetime import date

import pytest

from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots
from app.modules.temp_zone import EFFECTIVE_LAYER, transfer_lot

FAR = "2099-01-01"   # 不会过期的未来日期
PAST = "2000-01-01"  # 早已过期


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    return tmp_path


def _add_item(name="汤圆", layer="lower"):
    c = connect()
    cur = c.execute("INSERT INTO items(name,layer,unit) VALUES (?,?,?)", (name, layer, "袋"))
    c.commit(); iid = cur.lastrowid; c.close(); return iid


def _add_lot(item_id, qty=1, expiry=FAR, status="on_shelf"):
    c = connect()
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (item_id, qty, qty, expiry, status, "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return lid


def _eff_layer(lid):
    c = connect()
    r = c.execute(
        f"SELECT {EFFECTIVE_LAYER} L FROM lots JOIN items ON items.id=lots.item_id WHERE lots.id=?",
        (lid,)).fetchone()
    c.close(); return r["L"]


def _layer_membership(lid):
    """该 on_shelf lot 在每一层各出现几次——任一时刻必须恰好落在一层一次。"""
    c = connect()
    out = {}
    for L in ("upper", "mid", "lower"):
        out[L] = len(c.execute(
            f"SELECT 1 FROM lots JOIN items ON items.id=lots.item_id "
            f"WHERE lots.id=? AND lots.status='on_shelf' AND {EFFECTIVE_LAYER}=?", (lid, L)).fetchall())
    c.close(); return out


def _lot_row(lid):
    c = connect(); r = dict(c.execute("SELECT * FROM lots WHERE id=?", (lid,)).fetchone()); c.close(); return r


def _transfer_count():
    c = connect(); n = c.execute("SELECT COUNT(*) n FROM transfers").fetchone()["n"]; c.close(); return n


def test_transfer_moves_only_this_lot(db):
    iid = _add_item()
    a, b = _add_lot(iid), _add_lot(iid)  # 同品项两袋，都在下层
    c = connect()
    r = transfer_lot(c, a, "mid", "lower")
    c.close()
    assert r == {"ok": True, "lot_id": a, "from_layer": "lower", "to_layer": "mid"}
    assert _eff_layer(a) == "mid"
    assert _eff_layer(b) == "lower"            # 同品项其余批次不被一起改层
    assert _layer_membership(a) == {"upper": 0, "mid": 1, "lower": 0}  # 禁止跨层双挂
    assert _layer_membership(b) == {"upper": 0, "mid": 0, "lower": 1}
    c = connect()
    t = c.execute("SELECT * FROM transfers WHERE lot_id=?", (a,)).fetchone()
    assert (t["from_layer"], t["to_layer"]) == ("lower", "mid")
    c.close()


def test_expired_on_shelf_rejected_same_set_as_sweep(db):
    iid = _add_item()
    e = _add_lot(iid, expiry=PAST)  # 已过期但仍 on_shelf（下架扫描尚未跑到）
    before = _lot_row(e)
    c = connect()
    r = transfer_lot(c, e, "mid", "lower")
    c.close()
    assert r["ok"] is False and r["reason"] == "expired"
    assert _lot_row(e) == before  # 层位、余量原样，事务整体回滚
    # 与下架名单同一套：被转层拒绝的批正是 expire-sweep 会下架的批
    c = connect()
    on_shelf = [dict(x) for x in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
    c.close()
    assert e in expire_lots(on_shelf, date.today().isoformat())
    assert _transfer_count() == 0


def test_from_layer_guard(db):
    iid = _add_item()
    a = _add_lot(iid)
    c = connect()
    r = transfer_lot(c, a, "mid", "upper")  # 调用方看到的层与服务端不符 → 拒绝
    assert r["ok"] is False and r["reason"] == "layer_changed" and r["layer"] == "lower"
    r = transfer_lot(c, a, "mid", "lower")
    c.close()
    assert r["ok"] is True


def test_same_layer_and_bad_layer(db):
    iid = _add_item()
    a = _add_lot(iid)
    c = connect()
    assert transfer_lot(c, a, "lower")["reason"] == "same_layer"
    assert transfer_lot(c, a, "basement")["reason"] == "bad_layer"
    c.close()
    assert _eff_layer(a) == "lower" and _transfer_count() == 0


def test_not_on_shelf_and_empty_rejected(db):
    iid = _add_item()
    consumed = _add_lot(iid, status="consumed")
    empty = _add_lot(iid, qty=0)
    c = connect()
    r1 = transfer_lot(c, consumed, "mid")
    r2 = transfer_lot(c, empty, "mid")
    c.close()
    assert r1["reason"] == "not_on_shelf" and r2["reason"] == "empty"


def test_concurrent_transfers_serialize(db):
    """8 个并发转层（各自独立连接，与 consume/sweep 同一写锁）：恰好一个成功，
    其余因乐观守卫失败，最终该批只在中层、审计只有一条。"""
    iid = _add_item()
    a = _add_lot(iid)
    results = []

    def worker():
        c = connect()
        try:
            results.append(transfer_lot(c, a, "mid", "lower"))
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()

    wins = [r for r in results if r["ok"]]
    assert len(wins) == 1
    assert {r["reason"] for r in results if not r["ok"]} <= {"layer_changed", "same_layer"}
    assert _eff_layer(a) == "mid"
    assert _layer_membership(a) == {"upper": 0, "mid": 1, "lower": 0}
    assert _transfer_count() == 1


def test_consume_layer_scope_respects_override(db):
    """转到中层的批，按下层消费扣不到它；不按层消费（全品项 FEFO）才扣得到。"""
    iid = _add_item()
    a = _add_lot(iid, qty=1)
    c = connect()
    assert transfer_lot(c, a, "mid", "lower")["ok"] is True
    c.close()

    def scoped_consume(layer):
        c = connect()
        q = f"""SELECT lots.* FROM lots JOIN items ON items.id=lots.item_id
               WHERE lots.item_id=? AND lots.status='on_shelf' AND lots.qty_remain>0"""
        args = [iid]
        if layer:
            q += f" AND {EFFECTIVE_LAYER}=?"; args.append(layer)
        lots = [dict(r) for r in c.execute(q, args)]
        c.close()
        return consume_fefo(lots, 1)

    r = scoped_consume("lower")
    assert r["ok"] is False and r["reason"] == "short"  # 下层已没有可扣的批
    r = scoped_consume("mid")
    assert r["ok"] is True and r["deductions"][0]["lot_id"] == a
    r = scoped_consume(None)
    assert r["ok"] is True and r["deductions"][0]["lot_id"] == a


def test_failed_transfer_leaves_layer_qty_alerts_untouched(db):
    """失败时层位、余量、顶条紧急集合（由 lots 派生）一起留在转前状态。"""
    iid = _add_item()
    a = _add_lot(iid, qty=2)

    def alert_set():
        c = connect()
        rows = [dict(r) for r in c.execute(
            "SELECT id, expiry, qty_remain FROM lots WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL")]
        c.close()
        today = date.today().isoformat()
        from app.engines.fefo import is_expired
        return sorted((r["id"], "expired" if is_expired(r["expiry"], today) else "ok") for r in rows)

    before_row, before_alerts = _lot_row(a), alert_set()
    c = connect()
    assert transfer_lot(c, a, "mid", "upper")["ok"] is False   # layer_changed
    assert transfer_lot(c, a, "lower")["ok"] is False          # same_layer
    c.close()
    assert _lot_row(a) == before_row
    assert alert_set() == before_alerts
    assert _transfer_count() == 0
