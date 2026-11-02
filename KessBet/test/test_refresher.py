"""collect/refresher.py 的单元测试。用假 adapter，不依赖网络。"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from common.models import Match, Selection
from collect.refresher import Refresher

TEST_MATCH_ID = '__TEST_REFRESH__'


class FakeAdapter:
    """按预设返回盘口。"""
    def __init__(self, selections=None, delay=0.0, raise_on=None):
        self.selections = selections or []
        self.delay = delay
        self.raise_on = raise_on
        self.calls = []

    def fetch_odds(self, match_id, home='', away=''):
        self.calls.append(match_id)
        if self.delay:
            time.sleep(self.delay)
        if self.raise_on and match_id == self.raise_on:
            raise RuntimeError('boom')
        return list(self.selections)


class FakeDao:
    def __init__(self, match_row=None, db_count=0, existing=None):
        self.match_row = match_row
        self.db_count = db_count
        self.existing = existing or []      # [(market, side, line, sel, odds), ...]
        self.written = []

    def get_match(self, match_id):
        return self.match_row

    def count_odds(self, match_id):
        return self.db_count

    def get_selections(self, match_id):
        return self.existing

    def fetch_keys(self, match_id):
        return {(r[0], r[1], r[2], r[3]) for r in self.existing}

    def delete_keys(self, match_id, keys):
        return 0

    @property
    def conn(self):
        class C:
            def cursor(self_inner):
                class Cur:
                    def executemany(self_i, *a): pass
                    def __enter__(self_i): return self_i
                    def __exit__(self_i, *a): pass
                return Cur()
            def commit(self_inner): pass
            def rollback(self_inner): pass
        return C()


def _row(kickoff_offset_hours=24):
    ko = datetime.now(timezone.utc) + timedelta(hours=kickoff_offset_hours)
    return ('sr:sport:1', 'Home', 'Away', ko.replace(tzinfo=None))


def _sels(n=3):
    return [Selection('Total', '', f'{i}.5', 'over', 1.5 + i * 0.1)
            for i in range(n)]


# ---------- 脏队列 ----------

def test_mark_dirty_dedups():
    r = Refresher(adapter=FakeAdapter(), dao=FakeDao())
    r.mark_dirty('m1')
    r.mark_dirty('m1')
    r.mark_dirty('m2')
    assert r.pending == 2
    assert r.stats['marked'] == 3


def test_take_ready_respects_debounce():
    r = Refresher(adapter=FakeAdapter(), dao=FakeDao(), debounce=0.3)
    now = time.monotonic()
    r.mark_dirty('m1')
    assert r._take_ready(now + 0.1) == [], '防抖期内不应取出'
    ready = r._take_ready(now + 0.5)
    assert ready == ['m1']
    assert r.pending == 0


def test_take_ready_clears_queue():
    r = Refresher(adapter=FakeAdapter(), dao=FakeDao(), debounce=0)
    r.mark_dirty('m1')
    r.mark_dirty('m2')
    ready = r._take_ready(time.monotonic() + 1)
    assert set(ready) == {'m1', 'm2'}
    assert r.pending == 0


# ---------- 刷新 ----------

def test_refresh_skips_unknown_match():
    r = Refresher(adapter=FakeAdapter(), dao=FakeDao(match_row=None))
    assert r.refresh_one('unknown') == 0


def test_refresh_skips_after_kickoff():
    dao = FakeDao(match_row=_row(kickoff_offset_hours=-1))
    r = Refresher(adapter=FakeAdapter(_sels()), dao=dao)
    assert r.refresh_one(TEST_MATCH_ID) == 0
    assert r.stats['skipped_kickoff'] == 1


def test_refresh_handles_fetch_error():
    dao = FakeDao(match_row=_row())
    adapter = FakeAdapter(_sels(), raise_on=TEST_MATCH_ID)
    r = Refresher(adapter=adapter, dao=dao)
    assert r.refresh_one(TEST_MATCH_ID) == 0
    assert r.stats['failed'] == 1


def test_refresh_skips_empty_response():
    dao = FakeDao(match_row=_row())
    r = Refresher(adapter=FakeAdapter([]), dao=dao)
    assert r.refresh_one(TEST_MATCH_ID) == 0


def test_ratio_protection_blocks_overwrite():
    """接口返回远少于库内 —— 疑似残缺响应，不覆盖。"""
    dao = FakeDao(match_row=_row(), db_count=100)
    r = Refresher(adapter=FakeAdapter(_sels(3)), dao=dao)
    assert r.refresh_one(TEST_MATCH_ID) == 0
    assert r.stats['skipped_suspicious'] == 1


def test_ratio_allows_normal_response():
    """接口返回数量正常（>= 库内 30%），允许覆盖。"""
    dao = FakeDao(match_row=_row(), db_count=5)
    r = Refresher(adapter=FakeAdapter(_sels(3)), dao=dao)
    n = r.refresh_one(TEST_MATCH_ID)
    assert n == 3, '3 >= 5*0.3，应该允许写入'
    assert r.stats['refreshed'] == 1


def test_ratio_allows_first_write():
    """库里没有数据时，任何响应都允许写入。"""
    dao = FakeDao(match_row=_row(), db_count=0)
    r = Refresher(adapter=FakeAdapter(_sels(2)), dao=dao)
    assert r.refresh_one(TEST_MATCH_ID) == 2
