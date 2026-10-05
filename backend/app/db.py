import os, sqlite3
from contextlib import contextmanager
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"

def connect():
    # timeout 即 SQLite busy timeout：并发写者排队等待而不是直接报
    # "database is locked"。转层 / 消费 / 下架互相让行。
    c = sqlite3.connect(db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    return c

@contextmanager
def tx():
    """读-改-写工作单元。

    BEGIN IMMEDIATE 在进入事务时就拿写锁，把 consume / transfer /
    expire-sweep 的"先 SELECT 再 UPDATE"串行化，杜绝基于过期快照的
    扣减或转层。任何异常整体回滚：层位覆盖、余量、审计记录一起回到
    操作前；顶条紧急集合是 lots 的实时派生查询，随之自动一致。
    """
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()
