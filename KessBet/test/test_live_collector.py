"""collect/live.py 的测试。

聚焦采集逻辑本身，用 mock adapter 避免依赖真实网络和进行中的比赛。

运行: py -m pytest test/test_live_collector.py -v
"""
import os
import sys
from datetime import timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest

from collect.live import (LiveCollector, _drop_broken_markets,
                          _parse_minute, _same_snapshot_state, _to_int)
from common.models import Selection
from common.timeutil import now_beijing
from db.dao import Dao

MID = '_test_livec_'
PLATFORM = 'kessbet'


# ---------------------------------------------------------------- 辅助函数

def test_parse_minute():
    assert _parse_minute('65:54') == 65
    assert _parse_minute('90:04') == 90
    assert _parse_minute('0:30') == 0
    assert _parse_minute('') is None
    assert _parse_minute(None) is None
    assert _parse_minute('bad') is None
    assert _parse_minute('-') is None


def test_to_int_distinguishes_empty_from_zero():
    """'' 应该返回 None 而不是 0 —— 「没采到」和「0 个球」是两件事。"""
    assert _to_int('0') == 0, '真实的 0 要保留'


def test_snapshot_handler_can_be_attached_after_init():
    handler = lambda *_: None
    collector = LiveCollector(object(), object())

    collector.set_snapshot_handler(handler)

    assert collector._on_snapshot is handler
    assert _to_int(0) == 0
    assert _to_int('') is None
    assert _to_int(None) is None
    assert _to_int('-') is None
    assert _to_int('abc') is None


def test_snapshot_state_rejects_score_change():
    before = {'ms': 7, 'mt': '49:58', 'hmp': '0', 'amp': '0'}
    after = {'ms': 7, 'mt': '50:03', 'hmp': '1', 'amp': '0'}
    assert not _same_snapshot_state(before, after)


def test_snapshot_state_accepts_same_score_progress():
    before = {'ms': 7, 'mt': '49:58', 'hmp': '0', 'amp': '0'}
    after = {'ms': 7, 'mt': '50:03', 'hmp': '0', 'amp': '0'}
    assert _same_snapshot_state(before, after)


# ---------------------------------------------------------------- 采集逻辑

@pytest.fixture()
def dao():
    d = Dao()
    _purge(d)
    yield d
    _purge(d)
    d.close()


def _purge(d):
    with d.conn.cursor() as c:
        for t in ('kessbet_odds_live', 'kessbet_odds_live_history',
                  'kessbet_match_live', 'kessbet_match'):
            c.execute(f"DELETE FROM {t} WHERE platform=%s AND match_id=%s",
                      (PLATFORM, MID))
    d.conn.commit()


def _insert_match(dao, kickoff):
    with dao.conn.cursor() as c:
        c.execute("""INSERT INTO kessbet_match
                     (platform, match_id, sport, league, country,
                      home_team, away_team, kickoff_time,
                      first_seen_time, updated_time)
                     VALUES (%s,%s,'sr:sport:1','L','C','H','A',%s,NOW(),NOW())""",
                  (PLATFORM, MID, kickoff))
    dao.conn.commit()


class FakeAdapter:
    """可配置返回值的假 adapter。"""

    def __init__(self, state=None, odds=None, state_raises=None):
        self._state = state
        self._odds = odds or []
        self._state_raises = state_raises
        self.state_calls = 0
        self.odds_calls = 0

    def fetch_state(self, match_id):
        self.state_calls += 1
        if self._state_raises:
            raise self._state_raises
        return self._state

    def fetch_odds(self, match_id, home='', away=''):
        self.odds_calls += 1
        return self._odds


class StateSequenceAdapter(FakeAdapter):
    def __init__(self, states, odds):
        super().__init__(odds=odds)
        self._states = iter(states)

    def fetch_state(self, match_id):
        self.state_calls += 1
        return next(self._states)


def _meta(kickoff):
    return {'match_id': MID, 'home_team': 'H', 'away_team': 'A',
            'kickoff_time': kickoff}


def _sels():
    return [Selection('1x2', '', '', '1', 2.5),
            Selection('1x2', '', '', 'X', 3.2)]


# ---------------------------------------------------------------- 正常路径

def test_writes_state_and_odds(dao):
    kickoff = now_beijing() - timedelta(minutes=30)
    ad = FakeAdapter(
        state={'ms': 7, 'mt': '30:15', 'hmp': '1', 'amp': '0',
               'hck': 3, 'ack': 1, 'hyc': 0, 'ayc': 1, 'hrc': 0, 'arc': 0},
        odds=_sels())
    lc = LiveCollector(ad, dao)

    lc._collect_one(dao, _meta(kickoff))

    assert lc.stats['state_ok'] == 1
    assert lc.stats['odds_ok'] == 1
    assert lc.stats['history_rows'] == 2

    with dao.conn.cursor() as c:
        c.execute("""SELECT status, minute, home_score, home_corner
                     FROM kessbet_match_live WHERE match_id=%s""", (MID,))
        r = c.fetchone()
    assert r[0] == 7 and r[1] == 30 and r[2] == 1 and r[3] == 3


def test_score_change_during_fetch_requeues_without_writing_odds(dao):
    kickoff = now_beijing() - timedelta(minutes=50)
    _insert_match(dao, kickoff)
    ad = StateSequenceAdapter([
        {'ms': 7, 'mt': '49:58', 'hmp': '0', 'amp': '0'},
        {'ms': 7, 'mt': '50:03', 'hmp': '1', 'amp': '0'},
    ], _sels())
    called = []
    lc = LiveCollector(ad, dao, on_snapshot=lambda *_: called.append(True))

    lc._collect_one(dao, _meta(kickoff))

    assert lc.stats['inconsistent_snapshot'] == 1
    assert lc.stats['odds_ok'] == 0
    assert lc.pending_push == 1
    assert called == []
    with dao.conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM kessbet_odds_live WHERE match_id=%s", (MID,))
        assert c.fetchone()[0] == 0


def test_consistent_snapshot_is_analyzed_immediately(dao):
    kickoff = now_beijing() - timedelta(minutes=30)
    state = {'ms': 7, 'mt': '30:15', 'hmp': '0', 'amp': '0'}
    received = []
    lc = LiveCollector(
        StateSequenceAdapter([state, state], _sels()), dao,
        on_snapshot=lambda _dao, meta, rows: received.append((meta, rows)),
    )

    lc._collect_one(dao, _meta(kickoff))

    assert len(received) == 1
    meta, rows = received[0]
    assert meta['snapshot_id']
    assert meta['home_score'] == 0 and meta['away_score'] == 0
    assert meta['snapshot_selections'] == rows


def test_history_carries_score_context(dao):
    """滚球流水必须带比分 —— 这是它区别于赛前流水的核心。"""
    kickoff = now_beijing() - timedelta(minutes=45)
    ad = FakeAdapter(
        state={'ms': 7, 'mt': '45:00', 'hmp': '2', 'amp': '1',
               'hck': 0, 'ack': 0, 'hyc': 0, 'ayc': 0, 'hrc': 0, 'arc': 0},
        odds=_sels())
    lc = LiveCollector(ad, dao)
    lc._collect_one(dao, _meta(kickoff))

    with dao.conn.cursor() as c:
        c.execute("""SELECT minute, home_score, away_score
                     FROM kessbet_odds_live_history WHERE match_id=%s""", (MID,))
        rows = c.fetchall()
    assert len(rows) == 2
    assert all(r[0] == 45 and r[1] == 2 and r[2] == 1 for r in rows)


# ---------------------------------------------------------------- 结束与异常

def test_finished_when_state_is_none(dao):
    """接口返回 None = 比赛结束。"""
    kickoff = now_beijing() - timedelta(minutes=120)
    ad = FakeAdapter(state=None)
    lc = LiveCollector(ad, dao)

    lc._collect_one(dao, _meta(kickoff))

    assert lc.stats['finished'] == 1
    assert ad.odds_calls == 0, '结束后不该再拉赔率'


def test_finished_when_status_100(dao):
    kickoff = now_beijing() - timedelta(minutes=120)
    ad = FakeAdapter(state={'ms': 100, 'hmp': '1', 'amp': '1'})
    lc = LiveCollector(ad, dao)

    lc._collect_one(dao, _meta(kickoff))

    assert lc.stats['finished'] == 1
    assert ad.odds_calls == 0


def test_empty_odds_is_skipped(dao):
    """空赔率响应多半是接口故障，不能覆盖库里的数据。"""
    kickoff = now_beijing() - timedelta(minutes=30)
    ad = FakeAdapter(
        state={'ms': 7, 'mt': '30:00', 'hmp': '0', 'amp': '0'},
        odds=[])
    lc = LiveCollector(ad, dao)

    lc._collect_one(dao, _meta(kickoff))

    assert lc.stats['state_ok'] == 1
    assert lc.stats['skipped_empty'] == 1
    assert lc.stats['odds_ok'] == 0


def test_state_error_counted_not_raised(dao):
    """单场失败不能让整轮崩掉。"""
    kickoff = now_beijing() - timedelta(minutes=30)
    ad = FakeAdapter(state_raises=RuntimeError('boom'))
    lc = LiveCollector(ad, dao)

    lc._safe_collect(dao, _meta(kickoff))

    assert lc.stats['failed'] == 1


# ---------------------------------------------------------------- poll_once

def test_live_matches_scope(dao):
    """live_matches 的时间窗口判定。

    不用 poll_once 测 —— 那会扫全库，真实的滚球比赛会干扰断言。
    直接测 SQL 的窗口边界更准确。
    """
    now = now_beijing()
    _insert_match(dao, now - timedelta(minutes=30))
    ids = [r['match_id'] for r in dao.live_matches(150)]
    assert MID in ids, '进行中的应在窗口内'

    _purge(dao)
    _insert_match(dao, now + timedelta(hours=2))
    ids = [r['match_id'] for r in dao.live_matches(150)]
    assert MID not in ids, '未来开赛的不该在窗口内'

    _purge(dao)
    _insert_match(dao, now - timedelta(minutes=200))
    ids = [r['match_id'] for r in dao.live_matches(150)]
    assert MID not in ids, '超过窗口的不该在窗口内'


def test_poll_once_counts_matches(dao, monkeypatch):
    """poll_once 返回它处理的场次数，与 live_matches 一致。

    用 monkeypatch 把 _safe_collect 换成空操作 —— 只验计数，
    不打真实接口（库里有真实比赛在跑，打网络会让测试很慢）。
    """
    _insert_match(dao, now_beijing() - timedelta(minutes=30))
    ad = FakeAdapter()
    lc = LiveCollector(ad, dao)

    collected = []

    def fake_safe(dao_, m, fetch_odds=True):
        collected.append((m['match_id'], fetch_odds))

    def fake_own(m, fetch_odds=True):
        collected.append((m['match_id'], fetch_odds))

    monkeypatch.setattr(lc, '_safe_collect', fake_safe)
    monkeypatch.setattr(lc, '_collect_with_own_dao', fake_own)

    expected = len(dao.live_matches(150))
    n = lc.poll_once()
    assert n == expected
    assert lc.stats['polls'] == 1
    assert lc.stats['matches'] == expected
    assert MID in [mid for mid, _ in collected], '我们造的那场应该被处理'
    # 状态轮询不该拉赔率 —— 赔率走推送
    assert all(fo is False for _, fo in collected), \
        'poll_once 只刷状态，赔率应由 push_once 负责'


def test_collect_one_runs_for_our_match(dao):
    """针对我们造的那一场，验证采集确实发生。"""
    _insert_match(dao, now_beijing() - timedelta(minutes=30))
    ad = FakeAdapter(
        state={'ms': 7, 'mt': '30:00', 'hmp': '1', 'amp': '0',
               'hck': 0, 'ack': 0, 'hyc': 0, 'ayc': 0, 'hrc': 0, 'arc': 0},
        odds=_sels())
    lc = LiveCollector(ad, dao)

    meta = {'match_id': MID, 'home_team': 'H', 'away_team': 'A',
            'kickoff_time': now_beijing() - timedelta(minutes=30)}
    lc._safe_collect(dao, meta)

    with dao.conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM kessbet_match_live "
                  "WHERE platform=%s AND match_id=%s", (PLATFORM, MID))
        assert c.fetchone()[0] == 1


# ---------------------------------------------------------------- 状态码

def test_finished_status_codes():
    """终态集合要包含实测到的所有终态。"""
    from collect.live import FINISHED_STATUS
    assert 100 in FINISHED_STATUS, '常规时间结束'
    assert 120 in FINISHED_STATUS, '点球大战'
    assert 0 not in FINISHED_STATUS, '未开始/延期不是终态'
    assert 6 not in FINISHED_STATUS and 7 not in FINISHED_STATUS


def test_penalty_shootout_marked_finished(dao):
    """ms=120（点球大战）应被判为结束，不再采赔率。"""
    kickoff = now_beijing() - timedelta(minutes=130)
    ad = FakeAdapter(state={'ms': 120, 'mt': '', 'hmp': '6', 'amp': '5'})
    lc = LiveCollector(ad, dao)

    lc._collect_one(dao, _meta(kickoff))

    assert lc.stats['finished'] == 1
    assert ad.odds_calls == 0, '点球大战已经不算滚球了'


def test_status_6_is_in_play(dao):
    """ms=6 是进行中（实测有 mt 和比分）。"""
    kickoff = now_beijing() - timedelta(minutes=42)
    ad = FakeAdapter(
        state={'ms': 6, 'mt': '42:00', 'hmp': '1', 'amp': '0',
               'hck': 2, 'ack': 1, 'hyc': 0, 'ayc': 0, 'hrc': 0, 'arc': 0},
        odds=_sels())
    lc = LiveCollector(ad, dao)

    lc._collect_one(dao, _meta(kickoff))

    assert lc.stats['state_ok'] == 1
    assert lc.stats['odds_ok'] == 1
    assert lc.stats['finished'] == 0


# ---------------------------------------------------------------- 推送驱动

def test_push_once_ignores_out_of_window(dao, monkeypatch):
    """推送来的比赛如果不在滚球窗口，不该被处理。

    WS 会推所有订阅的比赛（含未开赛），滚球采集器要自己筛。
    """
    _insert_match(dao, now_beijing() + timedelta(hours=2))   # 未开赛
    ad = FakeAdapter()
    lc = LiveCollector(ad, dao)

    handled = []
    monkeypatch.setattr(lc, '_safe_collect',
                        lambda d, m, fetch_odds=True: handled.append(m['match_id']))
    monkeypatch.setattr(lc, '_collect_with_own_dao',
                        lambda m, fetch_odds=True: handled.append(m['match_id']))

    lc.mark_dirty(MID)
    n = lc.push_once()

    assert n == 0
    assert MID not in handled


def test_push_once_processes_in_window(dao, monkeypatch):
    """推送来的比赛在窗口内，要处理且拉赔率。"""
    _insert_match(dao, now_beijing() - timedelta(minutes=30))
    ad = FakeAdapter()
    lc = LiveCollector(ad, dao)

    handled = []
    monkeypatch.setattr(lc, '_safe_collect',
                        lambda d, m, fetch_odds=True: handled.append((m['match_id'], fetch_odds)))
    monkeypatch.setattr(lc, '_collect_with_own_dao',
                        lambda m, fetch_odds=True: handled.append((m['match_id'], fetch_odds)))

    lc.mark_dirty(MID)
    n = lc.push_once()

    assert n >= 1
    ours = [fo for mid, fo in handled if mid == MID]
    assert ours and ours[0] is True, '推送触发时要拉赔率'


def test_push_once_clears_dirty(dao):
    """处理过的推送要从脏标记里移除，避免重复处理。"""
    lc = LiveCollector(FakeAdapter(), dao)
    lc.mark_dirty(MID)
    assert lc.pending_push == 1
    lc.push_once()
    assert lc.pending_push == 0


def test_push_once_no_dirty_is_noop(dao):
    lc = LiveCollector(FakeAdapter(), dao)
    assert lc.push_once() == 0


# ---------- 异常盘口过滤 ----------

def test_drop_broken_markets_removes_placeholder_prices():
    """平台停盘时给的占位价必须剔除。

    实测（2026-11-13，Equatorial Guinea）：
        1st Half - Handicap 0:2   1@75.0   2@100.0   X@13.0
        Σ = 0.013 + 0.010 + 0.077 = 0.10

    两边主胜同时标 75/100 —— 平台在停盘时给的占位价，标记仍是可投注。
    留在库里会让套利判定算出 Σ=0.05 这种不存在机会。
    """
    rows = [
        ('1st Half - Handicap', '', '0:2', '1', 75.0),
        ('1st Half - Handicap', '', '0:2', '2', 100.0),
        ('1st Half - Handicap', '', '0:2', 'X', 13.0),
    ]
    assert _drop_broken_markets(rows) == []


def test_drop_broken_markets_keeps_normal():
    """正常抽水的盘口（Σ≈1.1）要保留。"""
    rows = [
        ('1x2', '', '', '1', 4.60),
        ('1x2', '', '', 'X', 3.05),
        ('1x2', '', '', '2', 1.75),
    ]   # Σ = 0.217+0.328+0.571 = 1.116
    assert len(_drop_broken_markets(rows)) == 3


def test_drop_broken_markets_per_line():
    """按 (market, side, line) 分组，不同盘线互不影响。"""
    rows = [
        ('Total', '', '2.5', 'over', 1.80),
        ('Total', '', '2.5', 'under', 2.00),      # Σ=1.056 正常
        ('Total', '', '3.5', 'over', 100.0),
        ('Total', '', '3.5', 'under', 100.0),     # Σ=0.02 异常
    ]
    out = _drop_broken_markets(rows)
    assert len(out) == 2
    assert all(r[2] == '2.5' for r in out)
