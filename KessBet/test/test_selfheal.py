"""采集进程自愈机制的测试。

背景：2026-10-29 17:56 发生 MySQL 连接失败（WinError 10013），
采集进程没有退出但停止了所有写入，僵死 50 分钟无人察觉。
这里测试"连续失败 → 主动退出"的保护。
"""
import os
import sys
from datetime import timedelta

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from collect.refresher import Refresher
from common.timeutil import now_beijing


class BoomAdapter:
    """每次都失败的 adapter。"""
    def fetch_odds(self, match_id, home='', away=''):
        raise RuntimeError('db down')


class FakeDao:
    def __init__(self):
        self.reset_count = 0

    def get_match(self, match_id):
        # 必须是未开赛的比赛，否则 refresh_one 会走「已开赛跳过」分支
        return ('sr:sport:1', 'Home', 'Away', now_beijing() + timedelta(hours=5))

    def count_odds(self, match_id):
        return 0

    def get_selections(self, match_id):
        return []

    def fetch_keys(self, match_id):
        return set()

    def delete_keys(self, match_id, keys):
        return 0

    def reset_connections(self):
        self.reset_count += 1

    @property
    def conn(self):
        class C:
            def cursor(self_inner):
                class Cur:
                    def execute(self_i, *a, **k): pass
                    def executemany(self_i, *a, **k): pass
                    def __enter__(self_i): return self_i
                    def __exit__(self_i, *a): pass
                return Cur()
            def commit(self_inner): pass
            def rollback(self_inner): pass
        return C()


# ---------- 连续失败计数 ----------

def test_consecutive_failures_counted():
    """连续失败要累计，成功则清零。"""
    r = Refresher(adapter=BoomAdapter(), dao=FakeDao(), debounce=0)
    for _ in range(3):
        r.refresh_one('m1')
    assert r.stats['consecutive_failures'] == 3, \
        f"应记录 3 次连续失败，实际 {r.stats['consecutive_failures']}"


def test_success_resets_failure_counter():
    """成功一次就把连续失败计数清零。"""
    class OkAdapter:
        def fetch_odds(self, match_id, home='', away=''):
            from common.models import Selection
            return [Selection('1x2', '', '', '1', 2.0)]

    dao = FakeDao()
    r = Refresher(adapter=BoomAdapter(), dao=dao, debounce=0)
    r.refresh_one('m1')
    r.refresh_one('m1')
    assert r.stats['consecutive_failures'] == 2

    r.adapter = OkAdapter()
    # OkAdapter 能成功，但 FakeDao.conn 是假的，写入会"成功"
    r.refresh_one('m1')
    assert r.stats['consecutive_failures'] == 0, '成功后应清零'


# ---------- 触发阈值 ----------

def test_reset_triggered_after_threshold():
    """连续失败达到阈值时，主动重启数据库连接。"""
    dao = FakeDao()
    r = Refresher(adapter=BoomAdapter(), dao=dao, debounce=0)
    # 默认阈值 20
    for _ in range(20):
        r.refresh_one('m1')
    assert dao.reset_count >= 1, '应触发 reset_connections'


def test_fatal_exit_after_max_failures():
    """连续失败达到硬上限时，抛出异常让进程退出。

    僵死不退出的进程比崩溃更危险——没人知道它停了。
    """
    r = Refresher(adapter=BoomAdapter(), dao=FakeDao(), debounce=0)
    max_f = r.max_consecutive_failures

    with pytest.raises(RuntimeError) as exc:
        for _ in range(max_f + 1):
            r.refresh_one('m1')
    assert '连续失败' in str(exc.value) or 'consecutive' in str(exc.value).lower()


def test_fatal_sets_exit_flag():
    """致命错误要置位 fatal 事件，主循环靠它决定退出。

    刷新线程是守护线程，异常抛出去主线程收不到——必须走显式信号。
    """
    r = Refresher(adapter=BoomAdapter(), dao=FakeDao(), debounce=0)
    assert not r.fatal.is_set()
    with pytest.raises(RuntimeError):
        for _ in range(r.max_consecutive_failures + 1):
            r.refresh_one('m1')
    assert r.fatal.is_set(), '应置位 fatal 事件'
    assert r.fatal_error and '连续失败' in r.fatal_error


def test_thresholds_are_sane():
    """阈值配置要合理：重置阈值 < 退出阈值。"""
    r = Refresher(adapter=BoomAdapter(), dao=FakeDao())
    assert 0 < r.failure_reset_threshold < r.max_consecutive_failures
    assert r.max_consecutive_failures >= 50, '阈值太低会误杀正常波动'
