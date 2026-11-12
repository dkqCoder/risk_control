"""WS delete 信号放行比例保护。

背景：比例保护（REFRESH_MIN_RATIO=0.3）防的是「接口残缺返回」，
但整场撤盘时接口返回 0 也会被它挡住 —— 几百个已失效选项永久留在
kessbet_odds_prematch，analyze 读到的就是死数据。

实测：
  245091926494187972  Philippine Army vs Azkals   库229 / 接口0
  245151063898887247  FK Babrungas vs Siauliai B  库265 / 接口0

WS 推送的 `delete` 字段明确告诉我们「哪些盘口被删了」，
这是平台撤盘的直接证据，与「接口残缺」性质完全不同。

契约：
  - 收到 delete 后，该场获得一次「可绕过比例保护」的许可
  - 许可只能用一次（下次刷新恢复正常判断，防止残缺响应永久放行）
  - 许可有时效（避免很早的 delete 影响后面的刷新）
  - 没有 delete 信号时，比例保护行为完全不变
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from common.models import Selection
from collect.refresher import Refresher

MID = '__TEST_DELETE__'


class FakeAdapter:
    def __init__(self, selections=None):
        self.selections = selections or []

    def fetch_odds(self, match_id, home='', away=''):
        return list(self.selections)


class FakeDao:
    def __init__(self, match_row=None, db_count=0, db_keys=None,
                 existing=None):
        self.match_row = match_row
        self.db_count = db_count
        self.db_keys = db_keys or set()
        self.existing = existing or []

    def get_match(self, match_id):
        return self.match_row

    def count_odds(self, match_id):
        return self.db_count

    def get_selections(self, match_id):
        return self.existing

    def fetch_keys(self, match_id):
        return set(self.db_keys)

    def delete_keys(self, match_id, keys):
        return len(keys)

    def log_removed(self, match_id, removed, removed_time=None, source='http'):
        self.removed = list(removed)
        self.removed_source = source
        return len(removed)

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


def _row(hours=24):
    ko = datetime.now(timezone.utc) + timedelta(hours=hours)
    return ('sr:sport:1', 'Home', 'Away', ko.replace(tzinfo=None))


def _sels(n):
    return [Selection('Total', '', f'{i}.5', 'over', 1.5) for i in range(n)]


def _refresher(db_count, api_n, marks=None, db_keys=None, existing=None):
    dao = FakeDao(match_row=_row(), db_count=db_count,
                  db_keys=db_keys, existing=existing)
    r = Refresher(adapter=FakeAdapter(_sels(api_n)), dao=dao)
    for m in (marks or []):
        r.mark_removed(m)
    return r, dao


# ---------- 基本行为 ----------

def test_delete_signal_clears_stale_rows_when_api_empty():
    """有 delete 信号且接口返回 0 时，必须清空库内的死数据。"""
    keys = {('1x2', '', '', '1'), ('1x2', '', '', 'X'), ('1x2', '', '', '2')}
    existing = [('1x2', '', '', '1', 2.0), ('1x2', '', '', 'X', 3.5),
                ('1x2', '', '', '2', 4.0)]
    r, dao = _refresher(db_count=229, api_n=0, marks=[MID],
                        db_keys=keys, existing=existing)

    r.refresh_one(MID)

    assert r.stats['skipped_suspicious'] == 0, '不应被比例保护拦下'
    assert dao.removed_source == 'ws', '撤盘日志来源应为 ws'
    assert len(dao.removed) == 3, '三个失效选项都要记进撤盘日志'


def test_delete_signal_bypasses_partial_removal():
    """部分撤盘（api=4 db=15）同样应该允许覆盖。"""
    r, _ = _refresher(db_count=15, api_n=4, marks=[MID])
    assert r.refresh_one(MID) == 4
    assert r.stats['skipped_suspicious'] == 0


def test_no_delete_signal_still_protected():
    """没有 delete 信号时，比例保护行为不变。"""
    r, _ = _refresher(db_count=100, api_n=3)
    assert r.refresh_one(MID) == 0
    assert r.stats['skipped_suspicious'] == 1


def test_delete_signal_for_other_match_does_not_leak():
    """别的比赛的 delete 信号不能放行这一场。"""
    r, _ = _refresher(db_count=100, api_n=3, marks=['other_match'])
    assert r.refresh_one(MID) == 0
    assert r.stats['skipped_suspicious'] == 1


# ---------- 信号生命周期 ----------

def test_delete_signal_consumed_after_one_refresh():
    """许可只能用一次 —— 下次刷新恢复比例保护。

    否则一次 delete 会让该场在后续所有残缺响应下都被放行。
    """
    r, _ = _refresher(db_count=100, api_n=3, marks=[MID])
    r.refresh_one(MID)                       # 第一次：放行
    r.stats['skipped_suspicious'] = 0
    r.refresh_one(MID)                       # 第二次：应恢复保护
    assert r.stats['skipped_suspicious'] == 1


def test_stale_delete_signal_expires():
    """过期的信号不能用（防止很久以前的 delete 影响现在的刷新）。"""
    r, _ = _refresher(db_count=100, api_n=3)
    r.mark_removed(MID)
    # 把信号时间推到很久以前
    r._removed_at[MID] = time.monotonic() - r.remove_signal_ttl - 1
    assert r.refresh_one(MID) == 0
    assert r.stats['skipped_suspicious'] == 1


def test_mark_removed_counts_stats():
    r = Refresher(adapter=FakeAdapter(), dao=FakeDao())
    r.mark_removed(MID)
    assert r.stats['removed_signals'] == 1
