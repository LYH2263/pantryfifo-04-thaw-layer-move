from app.db import connect

def init_db():
    c = connect()
    c.execute("PRAGMA journal_mode=WAL")  # 读写不互斥，设置一次即持久
    c.executescript("""
    CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, name TEXT, layer TEXT, unit TEXT);
    CREATE TABLE IF NOT EXISTS lots(
      id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INT, qty_in REAL, qty_remain REAL,
      expiry TEXT, status TEXT, data_quality TEXT, layer_override TEXT
    );
    CREATE TABLE IF NOT EXISTS consumptions(id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT, result_json TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS transfers(
      id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id INT, from_layer TEXT, to_layer TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    """)
    # 旧库轻量迁移：补 layer_override 列（NULL = 跟随品项默认层）
    cols = {r["name"] for r in c.execute("PRAGMA table_info(lots)")}
    if "layer_override" not in cols:
        c.execute("ALTER TABLE lots ADD COLUMN layer_override TEXT")
    if c.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 0:
        c.executemany("INSERT INTO items(name,layer,unit) VALUES (?,?,?)", [
            ("牛奶", "upper", "盒"), ("鸡蛋", "mid", "个"), ("冻饺", "lower", "袋"),
        ])
        c.executemany(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
            [
                (1, 2, 2, "2026-10-01", "on_shelf", "clean"),
                (1, 1, 1, "2026-09-28", "on_shelf", "clean"),
                (2, 12, 12, "2026-11-01", "on_shelf", "clean"),
                (3, 1, 1, "2025-01-01", "on_shelf", "dirty"),
                (2, -3, -3, "2026-12-01", "on_shelf", "dirty"),
            ],
        )
        c.execute("INSERT INTO settings(key,value) VALUES ('warn_days','3')")
    c.commit()
    c.close()
