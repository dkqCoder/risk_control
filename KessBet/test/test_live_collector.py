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

from analyze.live import run as L
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
                  'kessbet_odds_live_raw', 'kessbet_live_snapshot',
                  'kessbet_live_result_cost',
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


class RawAdapter(FakeAdapter):
    """同时返回原始 mari 串的 adapter（对应 KessbetAdapter 的新接口）。"""

    def fetch_odds_with_raw(self, match_id, home='', away=''):
        self.odds_calls += 1
        return self._odds, [{'mari': '1|m,1|1,2.5,1,1|1x2'}]


class ResultAdapter(FakeAdapter):
    """返回结构化 fetch_result + 身份列表，用于 Task 2 信封测试。"""

    def fetch_odds_result(self, match_id, home='', away=''):
        self.odds_calls += 1
        mari = '1|m,1|1,2.5,1,1|1x2'
        res = {
            'status': 'ok_nonempty', 'markets': [{'mari': mari}],
            'raw_body': '{"code":1,"data":[]}', 'response_status': 200,
            'response_headers': {'content-type': 'application/json'},
            'request': {'action': '0015', 'body': {'matchId': match_id}},
            'error': None, 'received_at': now_beijing(),
        }
        identity = [{'mari': mari, 'market': '1x2', 'side': '',
                     'line': '', 'label': '1', 'odds': 2.5,
                     'market_id': '1', 'locked': False}]
        return self._odds, res, identity


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


def test_saves_immutable_snapshot_and_passes_id(dao):
    """D01：raw、rows、state、请求时间绑定成同一快照，并传给即时分析。"""
    import json
    kickoff = now_beijing() - timedelta(minutes=30)
    ad = RawAdapter(
        state={'ms': 7, 'mt': '30:15', 'hmp': '1', 'amp': '0',
               'hck': 3, 'ack': 1, 'hyc': 0, 'ayc': 1, 'hrc': 0, 'arc': 0},
        odds=_sels())
    got = []
    lc = LiveCollector(ad, dao,
                       on_snapshot=lambda _d, meta, rows: got.append(meta))

    lc._collect_one(dao, _meta(kickoff))

    with dao.conn.cursor() as c:
        c.execute("""SELECT snapshot_id, generation, raw_payload, rows_json,
                            state_after
                     FROM kessbet_live_snapshot WHERE match_id=%s""", (MID,))
        rows = c.fetchall()
    assert len(rows) == 1
    sid, gen, raw_json, rows_json, state_after = rows[0]
    raw = json.loads(raw_json)
    assert raw['markets'][0]['mari'].endswith('|1x2')
    assert json.loads(state_after)['ms'] == 7
    assert json.loads(rows_json)

    with dao.conn.cursor() as c:
        c.execute("""SELECT current_snapshot_id, generation
                     FROM kessbet_match_live WHERE match_id=%s""", (MID,))
        current = c.fetchone()
    assert current == (sid, gen), '当前指针必须指向这份快照'

    assert got, '快照回调没有被调用'
    assert got[0]['snapshot_id'] == sid
    assert got[0]['generation'] == gen
    assert got[0]['raw_payload']


def test_halftime_snapshot_freezes_official_score(dao):
    kickoff = now_beijing() - timedelta(minutes=45)
    ad = RawAdapter(
        state={'ms': 31, 'mt': '45:00', 'hmp': '2', 'amp': '1'},
        odds=_sels())
    got = []
    lc = LiveCollector(ad, dao,
                       on_snapshot=lambda _d, meta, rows: got.append(meta))
    lc._collect_one(dao, _meta(kickoff))
    assert got
    assert (got[0]['ht_home'], got[0]['ht_away']) == (2, 1)
    assert got[0]['ht_source'] == 'official_state'


def _state():
    return {'ms': 7, 'mt': '30:15', 'hmp': '1', 'amp': '0',
            'hck': 3, 'ack': 1, 'hyc': 0, 'ayc': 1, 'hrc': 0, 'arc': 0}


def test_snapshot_stores_envelope_and_fingerprint(dao):
    """Task 2：请求/响应信封、哈希、指纹、解析版本都落快照。"""
    kickoff = now_beijing() - timedelta(minutes=30)
    lc = LiveCollector(ResultAdapter(state=_state(), odds=_sels()), dao)
    lc._collect_one(dao, _meta(kickoff))

    with dao.conn.cursor() as c:
        c.execute("""SELECT request_json, response_json, response_hash,
                            input_fingerprint, parser_version
                     FROM kessbet_live_snapshot WHERE match_id=%s""", (MID,))
        req, resp, rhash, fp, pver = c.fetchone()
    assert '0015' in (req or '')
    assert 'ok_nonempty' in (resp or '')
    assert len(rhash) == 64
    assert len(fp) == 64
    assert pver


def test_state_change_invalidates_and_refetches_immediately(dao):
    """P0：比分变化时旧快照失效，并在同一轮直接重取形成新快照。"""
    kickoff = now_beijing() - timedelta(minutes=30)
    st1 = _state()
    lc = LiveCollector(RawAdapter(state=st1, odds=_sels()), dao)
    lc._collect_one(dao, _meta(kickoff))
    with dao.conn.cursor() as c:
        c.execute("""SELECT current_snapshot_id, generation
                     FROM kessbet_match_live WHERE match_id=%s""", (MID,))
        sid1, gen1 = c.fetchone()
    assert sid1, '首次采集应有当前快照'

    st2 = dict(st1, hmp='2')
    lc2 = LiveCollector(ResultAdapter(state=st2, odds=_sels()), dao)
    lc2._collect_one(dao, _meta(kickoff), fetch_odds=False)

    with dao.conn.cursor() as c:
        c.execute("""SELECT current_snapshot_id, generation
                     FROM kessbet_match_live WHERE match_id=%s""", (MID,))
        sid2, gen2 = c.fetchone()
    assert sid2 and sid2 != sid1, '状态变化后应立即发布新快照'
    assert gen2 > gen1
    assert lc2.stats.get('state_changed_refetch') == 1


def test_same_score_poll_keeps_snapshot(dao):
    """比分没变的状态轮询不能把当前快照打空。"""
    kickoff = now_beijing() - timedelta(minutes=30)
    st = _state()
    lc = LiveCollector(RawAdapter(state=st, odds=_sels()), dao)
    lc._collect_one(dao, _meta(kickoff))
    with dao.conn.cursor() as c:
        c.execute("""SELECT current_snapshot_id FROM kessbet_match_live
                     WHERE match_id=%s""", (MID,))
        sid = c.fetchone()[0]

    lc2 = LiveCollector(FakeAdapter(state=dict(st)), dao)
    lc2._collect_one(dao, _meta(kickoff), fetch_odds=False)

    with dao.conn.cursor() as c:
        c.execute("""SELECT current_snapshot_id FROM kessbet_match_live
                     WHERE match_id=%s""", (MID,))
        assert c.fetchone()[0] == sid
    assert MID not in lc2._dirty


def test_empty_odds_invalidates_current_snapshot(dao):
    """P0：空响应不能继续认证旧快照。"""
    kickoff = now_beijing() - timedelta(minutes=30)
    lc = LiveCollector(RawAdapter(state=_state(), odds=_sels()), dao)
    lc._collect_one(dao, _meta(kickoff))

    lc2 = LiveCollector(FakeAdapter(state=_state(), odds=[]), dao)
    lc2._collect_one(dao, _meta(kickoff))

    with dao.conn.cursor() as c:
        c.execute("""SELECT current_snapshot_id FROM kessbet_match_live
                     WHERE match_id=%s""", (MID,))
        assert c.fetchone()[0] is None


def test_state_failure_invalidates_current_snapshot(dao):
    """P0：状态接口失败不能当比赛结束，但旧机会必须失效。"""
    kickoff = now_beijing() - timedelta(minutes=30)
    lc = LiveCollector(RawAdapter(state=_state(), odds=_sels()), dao)
    lc._collect_one(dao, _meta(kickoff))

    ad = StateSequenceAdapter([_state(), None], _sels())
    lc2 = LiveCollector(ad, dao)
    lc2._collect_one(dao, _meta(kickoff))

    with dao.conn.cursor() as c:
        c.execute("""SELECT current_snapshot_id FROM kessbet_match_live
                     WHERE match_id=%s""", (MID,))
        assert c.fetchone()[0] is None
    assert MID in lc2._dirty


def test_analyzer_uses_snapshot_id_not_current_pointer(dao):
    """D01：即使当前指针已指向新快照，按旧 snapshot_id 分析仍得到旧输入。"""
    kickoff = now_beijing() - timedelta(minutes=30)
    lc = LiveCollector(
        RawAdapter(state=_state(),
                   odds=[Selection('1x2', '', '', '1', 2.5),
                         Selection('1x2', '', '', 'X', 3.2)]),
        dao)
    lc._collect_one(dao, _meta(kickoff))
    with dao.conn.cursor() as c:
        c.execute("""SELECT snapshot_id, generation FROM kessbet_live_snapshot
                     WHERE match_id=%s ORDER BY generation""", (MID,))
        sid1, gen1 = c.fetchone()

    # 第二次采集把当前指针推进到新快照
    lc2 = LiveCollector(
        RawAdapter(state=_state(),
                   odds=[Selection('1x2', '', '', '1', 2.6),
                         Selection('1x2', '', '', 'X', 3.1)]),
        dao)
    lc2._collect_one(dao, _meta(kickoff))

    meta = _meta(kickoff)
    meta.update({'current_snapshot_id': sid1, 'generation': gen1})
    out = L.evaluate_live_match(dao, meta, now_beijing())

    assert out['snapshot_id'] == sid1
    assert out['selections'][0][4] == 2.5, '必须使用快照 A 的赔率'


def test_analyzer_without_snapshot_does_not_guess(dao):
    """迁移前/无快照的场次必须降级，不能按时间猜输入。"""
    out = L.evaluate_live_match(
        dao, _meta(now_beijing() - timedelta(minutes=30)), now_beijing())
    assert out['error'] == 'no_snapshot'
    assert out['result'] is None


def test_result_cost_older_generation_cannot_overwrite(dao):
    """D01：旧 generation 的分析晚完成时，不能覆盖新结果。"""
    kickoff = now_beijing() - timedelta(minutes=30)
    _insert_match(dao, kickoff)
    common = dict(
        match_id=MID, kickoff_time=kickoff, profit_pct=0.0, chosen='[]',
        reach_states=0, n_candidates=0, minute=30, home_score=0,
        away_score=0, ht_home=None, ht_away=None,
        odds_updated=now_beijing(), computed_time=now_beijing(),
    )

    dao.write_live_result_cost(cost=0.9, generation=2,
                              snapshot_id='snap-2', **common)
    dao.write_live_result_cost(cost=1.5, generation=1,
                              snapshot_id='snap-1', **common)

    with dao.conn.cursor() as c:
        c.execute("""SELECT cost, snapshot_id, generation
                     FROM kessbet_live_result_cost WHERE match_id=%s""",
                  (MID,))
        cost, sid, gen = c.fetchone()
    assert float(cost) == pytest.approx(0.9)
    assert sid == 'snap-2'
    assert gen == 2


def test_no_cover_status_overwrites_old_opportunity(dao):
    """P0：新快照没有覆盖结果时，旧成本必须被 NULL + 状态覆盖。"""
    kickoff = now_beijing() - timedelta(minutes=30)
    _insert_match(dao, kickoff)
    base = dict(
        match_id=MID, kickoff_time=kickoff, profit_pct=None, chosen='[]',
        reach_states=0, n_candidates=0, minute=30, home_score=0,
        away_score=0, ht_home=None, ht_away=None,
        odds_updated=now_beijing(), computed_time=now_beijing(),
    )
    dao.write_live_result_cost(cost=0.9, generation=1, snapshot_id='snap-1',
                              verification_status='verified',
                              analysis_status='valid', **base)
    dao.write_live_result_cost(cost=None, generation=2, snapshot_id='snap-2',
                              analysis_status='no_cover', **base)

    with dao.conn.cursor() as c:
        c.execute("""SELECT cost, snapshot_id, generation, analysis_status
                     FROM kessbet_live_result_cost WHERE match_id=%s""",
                  (MID,))
        cost, sid, gen, status = c.fetchone()
    assert cost is None, '旧成本必须被清掉'
    assert sid == 'snap-2' and gen == 2
    assert status == 'no_cover'


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

def test_state_none_is_failure_not_finished(dao):
    """P0：state=None 是接口失败，不是比赛结束；要失效并重试。"""
    kickoff = now_beijing() - timedelta(minutes=120)
    ad = FakeAdapter(state=None)
    lc = LiveCollector(ad, dao)

    lc._collect_one(dao, _meta(kickoff))

    assert lc.stats.get('state_failed') == 1
    assert lc.stats['finished'] == 0
    assert ad.odds_calls == 0
    assert MID in lc._dirty, '状态失败后应排队重试'


def test_safe_collect_failure_invalidates_current_snapshot(dao):
    """P0：采集抛异常时也要撤销当前机会并重试。"""
    kickoff = now_beijing() - timedelta(minutes=30)
    lc = LiveCollector(RawAdapter(state=_state(), odds=_sels()), dao)
    lc._collect_one(dao, _meta(kickoff))

    class BoomAdapter(FakeAdapter):
        def fetch_state(self, match_id):
            raise RuntimeError('boom')

    lc2 = LiveCollector(BoomAdapter(), dao)
    lc2._safe_collect(dao, _meta(kickoff))

    with dao.conn.cursor() as c:
        c.execute("""SELECT current_snapshot_id FROM kessbet_match_live
                     WHERE match_id=%s""", (MID,))
        assert c.fetchone()[0] is None
    assert lc2.stats['failed'] == 1
    assert MID in lc2._dirty


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
