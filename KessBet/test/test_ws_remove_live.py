"""Task 5：WS 删盘必须让滚球当前机会失效并排队重取。"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from collect.run import Collector


class _Refresh:
    def __init__(self):
        self.removed = []

    def mark_removed(self, mid):
        self.removed.append(mid)


class _Live:
    def __init__(self):
        self.dao = self
        self.invalidated = []
        self.dirty = []

    def invalidate_live_snapshot(self, mid):
        self.invalidated.append(mid)

    def mark_dirty(self, mid):
        self.dirty.append(mid)


def test_ws_remove_invalidates_live_current_opportunity():
    c = object.__new__(Collector)
    c.refresher = _Refresh()
    c.live = _Live()

    c._on_remove('m1')

    assert c.refresher.removed == ['m1']
    assert c.live.invalidated == ['m1']
    assert c.live.dirty == ['m1']
