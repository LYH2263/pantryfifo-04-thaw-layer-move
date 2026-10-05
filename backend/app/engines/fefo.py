"""FEFO consume: earliest expiry first among positive remaining lots."""

def is_expired(expiry: str | None, today: str) -> bool:
    """全系统唯一的"已过期"判定：下架名单、顶条 expired、转层资格共用。

    到期日当天即算过期（expiry <= today）。任何一处改口径都会让
    "顶条已当过期、层页还挂在架上"这类撕裂复现，所以只准改这里。
    """
    return bool(expiry) and expiry <= today

def sort_lots_fefo(lots: list[dict]) -> list[dict]:
    return sorted(
        [l for l in lots if float(l.get("qty_remain", 0)) > 0],
        key=lambda l: (l.get("expiry") or "9999-99-99", l.get("id") or 0),
    )

def consume_fefo(lots: list[dict], qty: float) -> dict:
    """Return deductions list and leftover demand. Mutates copies only."""
    need = float(qty)
    if need <= 0:
        return {"ok": False, "reason": "qty_non_positive", "deductions": [], "short": 0.0}
    ordered = sort_lots_fefo(lots)
    deductions = []
    for lot in ordered:
        if need <= 0:
            break
        avail = float(lot["qty_remain"])
        take = min(avail, need)
        deductions.append({"lot_id": lot["id"], "take": take, "expiry": lot.get("expiry")})
        need -= take
    if need > 1e-9:
        return {"ok": False, "reason": "short", "deductions": deductions, "short": round(need, 3)}
    return {"ok": True, "reason": "", "deductions": deductions, "short": 0.0}

def expire_lots(lots: list[dict], today: str) -> list[int]:
    """Ids that should leave shelf: remaining>0 and expired per is_expired."""
    return [l["id"] for l in lots
            if is_expired(l.get("expiry"), today) and float(l.get("qty_remain", 0)) > 0]
