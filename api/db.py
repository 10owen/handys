import os
from contextlib import contextmanager

import pymysql
import pymysql.cursors

CFG = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "staymate"),
    password=os.getenv("DB_PASSWORD", "staymate"),
    database=os.getenv("DB_NAME", "staymate"),
    charset="utf8mb4",
    cursorclass=pymysql.cursors.DictCursor,
    autocommit=False,
)


@contextmanager
def conn():
    """한 요청 = 한 트랜잭션. 예외면 롤백."""
    c = pymysql.connect(**CFG)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def q(c, sql, args=None):
    with c.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def q1(c, sql, args=None):
    with c.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()


def x(c, sql, args=None):
    with c.cursor() as cur:
        cur.execute(sql, args)
        return cur.lastrowid


def xmany(c, sql, rows):
    if not rows:
        return
    with c.cursor() as cur:
        cur.executemany(sql, rows)
