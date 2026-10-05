"""解冻转层：批级层位覆盖的资格一致性、失败回滚与并发不变量。

种子数据提醒：冻饺(item 3, lower)自带一批 2025-01-01 已过期在架批；
鸡蛋(item 2, mid)在架 12 个。测试日期一律相对 today，不硬编码。
"""
import threading
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import seed
from app.db import connect
from app.main import app


def d(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    with TestClient(app) as c:
        yield c


def add_lot(client, item_id=3, qty=2, days=30):
    r = client.post("/api/lots", json={"item_id": item_id, "qty": qty, "expiry": d(days)})
    assert r.status_code == 200
    return r.json()["id"]


def lot_row(lid):
    c = connect()
    r = c.execute("SELECT * FROM lots WHERE id=?", (lid,)).fetchone()
    c.close()
    return dict(r) if r else None


def transfer_rows():
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM transfers")]
    c.close()
    return rows


def layer_ids(client, layer):
    return {x["id"] for x in client.get(f"/api/fridge?layer={layer}").json()}


# ---------- 基本语义 ----------

def test_transfer_moves_single_lot_only(client):
    a = add_lot(client, qty=2)   # 冻饺两批同在 lower
    b = add_lot(client, qty=3)
    r = client.post(f"/api/lots/{a}/transfer", json={"to_layer": "mid", "from_layer": "lower"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "lot_id": a, "from_layer": "lower", "to_layer": "mid"}
    # 只有 a 到中层；b 与同品项默认层不动
    assert a in layer_ids(client, "mid") and a not in layer_ids(client, "lower")
    assert b in layer_ids(client, "lower") and b not in layer_ids(client, "mid")
    items = {i["id"]: i for i in client.get("/api/items").json()}
    assert items[3]["layer"] == "lower"           # 品项默认层不变
    assert lot_row(a)["layer_override"] == "mid"  # 覆盖只落在被转的批上
    assert lot_row(b)["layer_override"] is None


def test_transfer_logs_audit(client):
    lid = add_lot(client)
    assert client.post(f"/api/lots/{lid}/transfer", json={"to_layer": "mid"}).status_code == 200
    rows = transfer_rows()
    assert len(rows) == 1 and rows[0]["lot_id"] == lid
    assert rows[0]["from_layer"] == "lower" and rows[0]["to_layer"] == "mid"


def test_no_lot_in_two_layers(client):
    lid = add_lot(client)
    assert client.post(f"/api/lots/{lid}/transfer", json={"to_layer": "mid"}).status_code == 200
    seen = [layer_ids(client, L) for L in ("upper", "mid", "lower")]
    assert sum(lid in s for s in seen) == 1 and lid in seen[1]


def test_transfer_rejects_bad_layer_and_same_layer(client):
    lid = add_lot(client)
    assert client.post(f"/api/lots/{lid}/transfer", json={"to_layer": "attic"}).status_code == 409
    r = client.post(f"/api/lots/{lid}/transfer", json={"to_layer": "lower"})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "same_layer"
    assert lot_row(lid)["layer_override"] is None  # 失败不留痕
    assert transfer_rows() == []


# ---------- 过期资格：转层 / 下架名单 / 顶条 expired 同一套 ----------

def test_expired_lot_rejected_everywhere(client):
    lid = add_lot(client, days=-1)  # 昨天过期，仍在架
    alerts = client.get("/api/alerts").json()
    assert any(a["id"] == lid and a["level"] == "expired" for a in alerts)  # 顶条已当过期
    r = client.post(f"/api/lots/{lid}/transfer", json={"to_layer": "mid"})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "expired"  # 转层同口径拒绝
    assert lid in layer_ids(client, "lower")  # 没被偷偷转走
    swept = client.post("/api/expire-sweep").json()["expired_ids"]
    assert lid in swept  # 下架名单同一套
    r = client.post(f"/api/lots/{lid}/transfer", json={"to_layer": "mid"})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "not_on_shelf"


def test_expiry_today_counts_as_expired_everywhere(client):
    lid = add_lot(client, days=0)  # 到期日当天
    assert any(a["id"] == lid and a["level"] == "expired" for a in client.get("/api/alerts").json())
    assert client.post(f"/api/lots/{lid}/transfer", json={"to_layer": "mid"}).json()["detail"]["reason"] == "expired"
    assert lid in client.post("/api/expire-sweep").json()["expired_ids"]


# ---------- 与消费、下架交互时的失败回滚 ----------

def test_transfer_consumed_lot_fails(client):
    lid = add_lot(client, qty=1)
    # FEFO 先扣种子里的过期冻饺(1)，再扣新批(1) → 新批 consumed
    assert client.post("/api/consume", json={"item_id": 3, "qty": 2}).status_code == 200
    assert lot_row(lid)["status"] == "consumed"
    r = client.post(f"/api/lots/{lid}/transfer", json={"to_layer": "mid"})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "not_on_shelf"
    assert lot_row(lid)["layer_override"] is None and transfer_rows() == []


def test_transfer_layer_mismatch_rolls_back(client):
    lid = add_lot(client)
    before = client.get("/api/alerts").json()
    r = client.post(f"/api/lots/{lid}/transfer", json={"to_layer": "mid", "from_layer": "upper"})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "layer_mismatch"
    # 层位、审计、顶条紧急集合一起留在转前
    assert lot_row(lid)["layer_override"] is None
    assert transfer_rows() == []
    assert client.get("/api/alerts").json() == before
    # 用正确的 from_layer 仍可成功（没有被失败操作弄脏）
    assert client.post(f"/api/lots/{lid}/transfer",
                       json={"to_layer": "mid", "from_layer": "lower"}).status_code == 200


def test_consume_short_rolls_back(client):
    before = lot_row(3)  # 种子鸡蛋 12 个
    r = client.post("/api/consume", json={"item_id": 2, "qty": 999})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "short"
    assert lot_row(3)["qty_remain"] == before["qty_remain"]  # 余量未动


# ---------- 并发不变量 ----------

def test_concurrent_transfers_single_winner(client):
    lid = add_lot(client, qty=1)
    barrier, results = threading.Barrier(2), []

    def xfer(to):
        barrier.wait()
        r = client.post(f"/api/lots/{lid}/transfer", json={"to_layer": to, "from_layer": "lower"})
        results.append((to, r.status_code))

    ts = [threading.Thread(target=xfer, args=(t,)) for t in ("mid", "upper")]
    [t.start() for t in ts]; [t.join() for t in ts]
    wins = [to for to, code in results if code == 200]
    assert len(wins) == 1 and sorted(code for _, code in results) == [200, 409]
    assert lot_row(lid)["layer_override"] == wins[0]
    assert len(transfer_rows()) == 1
    seen = [layer_ids(client, L) for L in ("upper", "mid", "lower")]
    assert sum(lid in s for s in seen) == 1  # 同 lot 绝不同时挂两层


def test_concurrent_consume_and_transfer(client):
    for _ in range(3):
        lid = add_lot(client, item_id=2, qty=5, days=60)  # 鸡蛋新批，mid
        barrier, results = threading.Barrier(2), {}
        # 每轮把 item2 在架总量全扣掉（首轮含种子 12 个，之后只剩新批），
        # 保证新批一定被 FEFO 扣到 consumed
        total = sum(x["qty_remain"] for x in client.get("/api/fridge").json()
                    if x["item_id"] == 2 and x["qty_remain"] > 0)

        def do_consume():
            barrier.wait()
            results["consume"] = client.post("/api/consume", json={"item_id": 2, "qty": total}).status_code

        def do_transfer():
            barrier.wait()
            results["transfer"] = client.post(
                f"/api/lots/{lid}/transfer", json={"to_layer": "lower", "from_layer": "mid"}).status_code

        ts = [threading.Thread(target=f) for f in (do_consume, do_transfer)]
        [t.start() for t in ts]; [t.join() for t in ts]
        assert results["consume"] == 200            # 库存够，消费恒成功
        assert results["transfer"] in (200, 409)    # 取决于谁先拿到写锁
        row = lot_row(lid)
        assert row["status"] == "consumed" and row["qty_remain"] == 0  # 无负库存、无半扣
        if results["transfer"] == 200:              # 转层赢过消费 ⇒ 覆盖已落且只落一次
            assert row["layer_override"] == "lower"
        else:                                       # 消费先完成 ⇒ 转层必须空手而归
            assert row["layer_override"] is None and transfer_rows() == []
