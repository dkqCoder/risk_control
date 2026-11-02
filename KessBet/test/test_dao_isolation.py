"""db/dao.py 连接隔离级别的回归测试。用真实 MySQL。

背景：MySQL 默认 isolation 是 REPEATABLE READ。一条连接读到第一行后会固定
一致性快照，之后不 commit 就永远看不到别处的写入。本项目的读取方都是长期
存活的只读循环（analyze.run 每轮只读、从不 commit；collect.run 的 report()
只做查询），所以曾经出现：

  - analyze_once 第二轮起看到的永远是第一轮的赔率
  - collect.run --once 结束时 report() 报 prematch=0，库里实际有 5 万多行

修复方式：连接建立时 SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED。
这里验证两点：连接确实是 READ COMMITTED，且长连接能读到别的连接的提交。
"""
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pymysql

from db.dao import Dao

TEST_MATCH_ID = '__TEST_ISOLATION__'


@pytest.fixture
def dao():
    d = Dao()
    _purge(d)
    yield d
    _purge(d)
    d.close()


def _purge(d):
    with d.conn.cursor() as c:
        c.execute(
            "DELETE FROM kessbet_odds_history "
            "WHERE platform='kessbet' AND match_id=%s", (TEST_MATCH_ID,))
    d.conn.commit()


def _insert_via_other_connection(dao):
    """用另一条连接插入并提交，模拟采集进程的并发写入。"""
    other = pymysql.connect(**dao._dsn)
    try:
        with other.cursor() as c:
            c.execute(
                "INSERT INTO kessbet_odds_history "
                "(platform, match_id, sport, market, side, line, selection, "
                " odds, prev_odds, kickoff_time, changed_time) "
                "VALUES ('kessbet',%s,'sr:sport:1','1x2','','','1',"
                "        2.0,NULL,'2030-01-01 00:00:00',NOW())",
                (TEST_MATCH_ID,))
        other.commit()
    finally:
        other.close()


def _count(dao):
    with dao.conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM kessbet_odds_history "
                  "WHERE platform='kessbet' AND match_id=%s", (TEST_MATCH_ID,))
        return c.fetchone()[0]


def test_session_is_read_committed(dao):
    with dao.conn.cursor() as c:
        c.execute("SELECT @@session.transaction_isolation")
        assert c.fetchone()[0] == 'READ-COMMITTED'


def test_long_lived_connection_sees_other_commits(dao):
    """读一次（开启事务）后，别的连接提交的数据必须能读到。

    这正是 REPEATABLE READ 下会失败、导致分析进程冻结在首轮数据的场景。
    """
    assert _count(dao) == 0

    _insert_via_other_connection(dao)     # dao 的连接此时已有事务快照

    assert _count(dao) == 1


def test_repeat_queries_track_inserts(dao):
    """连续查询能跟上并发写入，不需要显式 commit。"""
    seen = [_count(dao)]
    for _ in range(3):
        _insert_via_other_connection(dao)
        seen.append(_count(dao))
    assert seen == [0, 1, 2, 3]
