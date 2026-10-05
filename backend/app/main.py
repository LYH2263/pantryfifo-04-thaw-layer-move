import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect, tx
from app.engines.fefo import consume_fefo, expire_lots, is_expired
from app.modules.temp_zone import EFFECTIVE_LAYER, transfer_lot

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    # layer 一律指有效层位 COALESCE(lots.layer_override, items.layer)：
    # 批级覆盖与品项默认层在此合一，层页/全层页看到的就是消费与顶条看到的。
    c = connect()
    q = f"""SELECT lots.*, items.name, {EFFECTIVE_LAYER} AS layer, items.unit FROM lots
            JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += f" AND {EFFECTIVE_LAYER}=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]; c.close()
    today = date.today().isoformat()
    for r in rows:
        r["expired"] = is_expired(r["expiry"], today)
    return rows

@app.get("/api/alerts")
def alerts():
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        f"""SELECT lots.*, items.name, {EFFECTIVE_LAYER} AS layer FROM lots JOIN items ON items.id=lots.item_id
            WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    c.close()
    out = []
    for r in rows:
        if is_expired(r["expiry"], today):
            r["level"] = "expired"
            out.append(r)
        else:
            delta = (date.fromisoformat(r["expiry"]) - date.today()).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    # BEGIN IMMEDIATE 事务：选批、扣减、记账在同一工作单元里，
    # 与转层、下架串行；任何一步失败余量整体回滚。
    with tx() as c:
        lots = [dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0", (body.item_id,))]
        result = consume_fefo(lots, body.qty)
        if not result["ok"] and result["reason"] == "qty_non_positive":
            raise HTTPException(400, result["reason"])
        if not result["ok"]:
            raise HTTPException(409, result)
        for d in result["deductions"]:
            cur = c.execute(
                "UPDATE lots SET qty_remain = qty_remain - ? WHERE id=? AND qty_remain>=?",
                (d["take"], d["lot_id"], d["take"]))
            if cur.rowcount != 1:  # 兜底：并发下宁可整单回滚也不留半扣状态
                raise HTTPException(409, {"reason": "conflict", "lot_id": d["lot_id"]})
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
            if rem <= 0:
                c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
    return result

class TransferIn(BaseModel):
    to_layer: str
    from_layer: str | None = None  # 乐观校验：调用方眼中的当前层，不符即 409

@app.post("/api/lots/{lot_id}/transfer")
def transfer(lot_id: int, body: TransferIn):
    # 转层确认与消费、过期下架共用 BEGIN IMMEDIATE 事务串行化；
    # 资格不符或审计写入失败都整体回滚：层位、余量、顶条集合一起回到转前。
    with tx() as c:
        result = transfer_lot(c, lot_id, body.to_layer, date.today().isoformat(), body.from_layer)
        if not result["ok"]:
            if result["reason"] == "not_found":
                raise HTTPException(404, result)
            raise HTTPException(409, result)
        c.execute(
            "INSERT INTO transfers(lot_id,from_layer,to_layer,created_at) VALUES (?,?,?,?)",
            (lot_id, result["from_layer"], result["to_layer"], datetime.now(timezone.utc).isoformat()))
    return result

@app.post("/api/expire-sweep")
def expire_sweep():
    with tx() as c:
        lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
        ids = expire_lots(lots, date.today().isoformat())
        for i in ids:
            c.execute("UPDATE lots SET status='expired' WHERE id=? AND status='on_shelf'", (i,))
    return {"expired_ids": ids}

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
