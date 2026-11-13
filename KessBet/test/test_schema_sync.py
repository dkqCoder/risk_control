"""表结构与 schema.sql 的一致性检查。

背景：db/schema.sql 全部是 `CREATE TABLE IF NOT EXISTS`，已存在的表
**不会**因为文件里加了新列而自动 ALTER。部署时只跑 schema.sql 会漏列，
直到某条 SQL 引用它才炸：

  2026-11-12  /api/arbitrage 500
  pymysql.err.OperationalError: (1054, "Unknown column 'is_live'")

这个测试把「schema.sql 声明的列」和「库里实际的列」逐表比对，
让漏列在部署阶段暴露，而不是等页面报错。
"""
import os
import re
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from db.dao import Dao

SCHEMA = os.path.join(BASE, 'db', 'schema.sql')

# schema.sql 里出现在列定义位置的类型关键字
TYPE_RE = (r'(?:VARCHAR|CHAR|DATETIME|DATE|DOUBLE|DECIMAL|INT|INTEGER|'
           r'BIGINT|SMALLINT|TINYINT|TEXT|BLOB|JSON)')


def _declared():
    """{表名: [列名, ...]}，从 schema.sql 解析。"""
    sql = open(SCHEMA, encoding='utf-8').read()
    out = {}
    for m in re.finditer(
            r'CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\) ENGINE', sql, re.S):
        table, body = m.group(1), m.group(2)
        cols = re.findall(rf'^    ([a-z_][a-z0-9_]*)\s+{TYPE_RE}', body, re.M)
        out[table] = cols
    return out


def _actual(dao, table):
    with dao.conn.cursor() as c:
        c.execute("""SELECT column_name FROM information_schema.columns
                     WHERE table_schema=DATABASE() AND table_name=%s
                     ORDER BY ordinal_position""", (table,))
        return [r[0] for r in c.fetchall()]


@pytest.fixture(scope='module')
def dao():
    d = Dao()
    yield d
    d.close()


def test_all_tables_exist(dao):
    """schema.sql 里的每张表都得建出来。"""
    missing = [t for t in _declared() if not _actual(dao, t)]
    assert not missing, f'缺表: {missing}（跑 db/schema.sql）'


def test_no_missing_columns(dao):
    """每张表的列必须覆盖 schema.sql 的声明。

    这条测试就是为 2026-11-12 的 is_live 事故写的：CREATE TABLE IF NOT
    EXISTS 不会给已存在的表加列，漏了要手工 ALTER。
    """
    problems = []
    for table, cols in _declared().items():
        have = _actual(dao, table)
        if not have:
            continue
        miss = [c for c in cols if c not in have]
        if miss:
            problems.append(f'{table} 缺列 {miss}')
    assert not problems, '\n'.join(problems) + '\n需要手工 ALTER TABLE'
