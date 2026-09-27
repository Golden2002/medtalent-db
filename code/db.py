# -*- coding: utf-8 -*-
"""
code/db.py —— 统一数据库访问层（psycopg 3）

为什么单独抽一层：
  · 连接参数集中一处，避免散落在各脚本里；
  · 统一 UTF-8 客户端编码（本机 Windows 控制台是 GBK，中文极易踩坑）；
  · 统一把业务异常翻译成可读信息。

连接：127.0.0.1:55432 / medtalent / postgres（本地 trust，仅回环）
"""
from __future__ import annotations

import contextlib
import os
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get(
    "MEDTALENT_DSN",
    "host=127.0.0.1 port=55432 dbname=medtalent user=postgres "
    "client_encoding=UTF8 options='-c search_path=mt,public'",
)


def connect(autocommit: bool = False):
    """返回一个 psycopg 连接（默认手动提交）。"""
    return psycopg.connect(DSN, autocommit=autocommit, row_factory=dict_row)


@contextlib.contextmanager
def cursor(autocommit: bool = False):
    conn = connect(autocommit=autocommit)
    try:
        with conn.cursor() as cur:
            yield cur
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def query_one(sql: str, params: Sequence[Any] | None = None) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def scalar(sql: str, params: Sequence[Any] | None = None) -> Any:
    row = query_one(sql, params)
    if row is None:
        return None
    return list(row.values())[0]


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def call(func: str, params: Iterable[Any] = ()) -> Any:
    """调用 mt schema 下的函数，返回其返回值。"""
    placeholders = ",".join(["%s"] * len(list(params)))
    with cursor() as cur:
        cur.execute("SELECT mt.%s(%s)" % (func, placeholders), list(params))
        row = cur.fetchone()
        return list(row.values())[0] if row else None


def health() -> dict:
    r = query_one("SELECT version() AS v, current_database() AS db, current_schema() AS sch")
    return r or {}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(health())
