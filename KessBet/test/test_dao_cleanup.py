"""滚球/成本相关表的清理覆盖。

背景：main 上 kessbet_live_scope_analysis、kessbet_market_removed 和
kessbet_match_cost 一直没有被清理；其中 match_cost 虽然有 cleanup_cost()，
但没有任何调用点，表会无限增长。

这里用假连接记录实际发出的 SQL，不连真实数据库。
"""
import os
import sys
import threading

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from db.dao import Dao


class _FakeCursor:
    def __init__(self, log):
        self.log = log
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((' '.join(sql.split()), params))
        self.rowcount = 0

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self):
        self.log = []

    def cursor(self):
        return _FakeCursor(self.log)

    def commit(self):
        pass

    def ping(self, reconnect=False):
        return None


def _dao():
    d = object.__new__(Dao)
    d._local = threading.local()
    d._local.conn = _FakeConn()
    d._local.epoch = 0
    d._epoch = 0
    return d


def _params_for(log, table):
    return [params for sql, params in log if table in sql]


def test_cleanup_live_deletes_scope_analysis():
    """影子分范围分析结果按 scope_days 清理。"""
    d = _dao()
    d.cleanup_live(scope_days=2)
    params = _params_for(d.conn.log, 'kessbet_live_scope_analysis')
    assert params, '没有清理 kessbet_live_scope_analysis'
    assert params[-1] == (2,)


def test_cleanup_live_deletes_market_removed():
    """撤盘日志按 removed_days 清理。"""
    d = _dao()
    d.cleanup_live(removed_days=7)
    params = _params_for(d.conn.log, 'kessbet_market_removed')
    assert params, '没有清理 kessbet_market_removed'
    assert params[-1] == (7,)


def test_cleanup_live_snapshot_uses_configured_days_and_keeps_guard():
    """快照保留期可配置，但证据排除和分批删除必须保留。"""
    d = _dao()
    d.cleanup_live(snapshot_days=7)
    hits = [(sql, params) for sql, params in d.conn.log
            if 'DELETE FROM kessbet_live_snapshot' in sql]
    assert hits, '没有清理 kessbet_live_snapshot'
    sql, params = hits[0]
    assert ('kessbet_arbitrage_snapshot' in sql
            or 'evidence_id' in sql), '快照清理必须排除证据引用'
    assert 'LIMIT 5000' in sql, '快照清理必须分批'
    assert params == (7,), 'snapshot_days 没有生效'


def test_cleanup_deletes_match_cost():
    """cleanup() 必须清理 kessbet_match_cost，否则只增不减。"""
    d = _dao()
    d.cleanup(match_days=7)
    # 必须带 WHERE：否则会误匹配 kessbet_match_cost_history。
    params = _params_for(d.conn.log, 'DELETE FROM kessbet_match_cost WHERE')
    assert params, 'cleanup() 没有清理 kessbet_match_cost'
    assert params[-1] == (7,)
