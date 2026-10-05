from app.engines.fefo import consume_fefo, expire_lots, is_expired, sort_lots_fefo

def test_fefo_order():
    lots = [
        {"id": 2, "qty_remain": 3, "expiry": "2026-02-01"},
        {"id": 1, "qty_remain": 2, "expiry": "2026-01-10"},
    ]
    assert [l["id"] for l in sort_lots_fefo(lots)] == [1, 2]
    r = consume_fefo(lots, 3)
    assert r["ok"] and r["deductions"][0]["lot_id"] == 1 and r["deductions"][0]["take"] == 2
    assert r["deductions"][1]["take"] == 1

def test_short():
    r = consume_fefo([{"id": 1, "qty_remain": 1, "expiry": "2026-01-01"}], 5)
    assert r["ok"] is False and r["short"] == 4

def test_expire():
    ids = expire_lots([
        {"id": 1, "qty_remain": 1, "expiry": "2025-01-01"},
        {"id": 2, "qty_remain": 1, "expiry": "2027-01-01"},
    ], "2026-01-01")
    assert ids == [1]

def test_is_expired_boundary():
    # 到期日当天不算过期（仍可消费/属临期），过了到期日才算；顶条、下架、转层共用此边界
    assert is_expired("2026-10-04", "2026-10-05") is True
    assert is_expired("2026-10-05", "2026-10-05") is False
    assert is_expired(None, "2026-10-05") is False
