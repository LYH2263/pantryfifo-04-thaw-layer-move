import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect, write_txn
from app.engines.fefo import consume_fefo, expire_lots, is_expired
from app.modules.temp_zone import EFFECTIVE_LAYER, transfer_lot

app = FastAPI(title="Pantryfifo", version="0.2.0")
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
    # layer 一律指有效层（批级覆盖优先）：转层后的批只出现在目标层，
    # 同品项其余批次仍按各自有效层归属，一个 lot id 不会同时落在两层。
    c = connect()
    q = f"""SELECT lots.*, items.name, {EFFECTIVE_LAYER} AS layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += f" AND {EFFECTIVE_LAYER}=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]; c.close(); return rows

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
            # 与 expire-sweep 下架名单、转层拒绝同一谓词，顶条不会和层页/下架打架
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
    layer: str | None = None  # 传了只从该有效层扣；解冻转走的批不会被当下层库存扣掉
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    try:
        with write_txn(c):
            q = f"""SELECT lots.*, {EFFECTIVE_LAYER} AS layer FROM lots
                   JOIN items ON items.id=lots.item_id
                   WHERE lots.item_id=? AND lots.status='on_shelf' AND lots.qty_remain>0"""
            args = [body.item_id]
            if body.layer:
                q += f" AND {EFFECTIVE_LAYER}=?"; args.append(body.layer)
            lots = [dict(r) for r in c.execute(q, args)]
            result = consume_fefo(lots, body.qty)
            if not result["ok"] and result["reason"] == "qty_non_positive":
                raise HTTPException(400, result["reason"])
            if not result["ok"]:
                raise HTTPException(409, result)
            for d in result["deductions"]:
                c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
                rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
                if rem <= 0:
                    c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
            c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                      (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        return result
    finally:
        c.close()

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    try:
        with write_txn(c):
            lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
            ids = expire_lots(lots, date.today().isoformat())
            for i in ids:
                c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
        return {"expired_ids": ids}
    finally:
        c.close()

class TransferIn(BaseModel):
    lot_id: int
    to_layer: str
    from_layer: str | None = None  # 乐观并发守卫：调用方看到的当前层

@app.post("/api/transfer")
def transfer(body: TransferIn):
    c = connect()
    try:
        result = transfer_lot(c, body.lot_id, body.to_layer, body.from_layer)
    finally:
        c.close()
    if not result["ok"]:
        code = {"lot_not_found": 404, "bad_layer": 400}.get(result["reason"], 409)
        raise HTTPException(code, result)
    return result

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows
