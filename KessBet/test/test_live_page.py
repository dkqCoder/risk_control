"""滚球页面的 API 测试（详情 + 走势）。

不测 HTML 本身 —— 那是纯前端，改样式不该让测试变红。
这里只保证页面依赖的数据接口不会 500、字段齐全。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest

from fastapi.testclient import TestClient

from db.dao import Dao, beijing_now
from web.api import app

MID = '_test_live_page_'
PLATFORM = 'kessbet'


@pytest.fixture(scope='module')
def client():
    return TestClient(app)


@pytest.fixture()
def seeded():
    d = Dao()
    _purge(d)
    try:
        with d.conn.cursor() as c:
            c.execute("""INSERT INTO kessbet_match
                         (platform, match_id, sport, league, country,
                          home_team, away_team, kickoff_time,
                          first_seen_time, updated_time)
                         VALUES (%s,%s,'sr:sport:1','L','C','H','A',
                                 NOW() - INTERVAL 60 MINUTE, NOW(), NOW())""",
                      (PLATFORM, MID))
            c.execute("""INSERT INTO kessbet_match_live
                         (platform, match_id, status, minute, home_score,
                          away_score, home_corner, away_corner,
                          home_yellow, away_yellow, home_red, away_red,
                          kickoff_time, fetched_time)
                         VALUES (%s,%s,31,55,1,0,5,2,1,0,0,0,
                                 NOW() - INTERVAL 60 MINUTE, NOW())""",
                      (PLATFORM, MID))
            # 赔率 5 列齐全，外加 prev_odds —— 详情接口曾经的 bug 就在
            # 这里：SELECT 带了 prev_odds，group_markets 解包报 ValueError
            for mkt, side, line, sel, odds in [
                ('1x2', '', '', '1', 1.85),
                ('1x2', '', '', 'X', 3.50),
                ('1x2', '', '', '2', 3.85),
                ('Total', '', '2.5', 'over', 1.80),
                ('Total', '', '2.5', 'under', 2.00),
            ]:
                c.execute("""INSERT INTO kessbet_odds_live
                             (platform, match_id, market, side, line, selection,
                              odds, prev_odds, kickoff_time, updated_time)
                             VALUES (%s,%s,%s,%s,%s,%s,%s,1.90,
                                     NOW() - INTERVAL 60 MINUTE, NOW())""",
                          (PLATFORM, MID, mkt, side, line, sel, odds))
        d.conn.commit()


        # 走势数据：三笔变动，其中一笔带比分。
        # 必须在 yield 之前写完 —— 放在 yield 之后是 fixture 的
        # teardown 阶段，测试早就跑完了，这个测试会永远看到 0 个点。
        with d.conn.cursor() as c:
            for odds, prev, minute, hs, as_ in [
                (1.90, None, 50, 0, 0),
                (1.85, 1.90, 55, 1, 0),
                (1.80, 1.85, 58, 1, 0),
            ]:
                c.execute("""INSERT INTO kessbet_odds_live_history
                             (platform, match_id, market, side, line, selection,
                              odds, prev_odds, minute, home_score, away_score,
                              kickoff_time, changed_time)
                             VALUES (%s,%s,'1x2','','','1',%s,%s,%s,%s,%s,
                                     NOW() - INTERVAL 60 MINUTE, NOW())""",
                          (PLATFORM, MID, odds, prev, minute, hs, as_))
        d.conn.commit()

        yield d
    finally:
        pass


def _purge(d):
    with d.conn.cursor() as c:
        for t in ('kessbet_odds_live', 'kessbet_odds_live_history',
                  'kessbet_live_result_cost', 'kessbet_live_snapshot',
                  'kessbet_match_live',
                  'kessbet_match'):
            c.execute(f"DELETE FROM {t} WHERE platform=%s AND match_id=%s",
                      (PLATFORM, MID))
    d.conn.commit()


@pytest.fixture()
def clean(seeded):
    d = seeded
    yield d
    _purge(d)
    d.close()


# ---------------------------------------------------------------- 详情

def test_live_detail_returns_markets(client, clean):
    """详情必须返回盘口 —— 之前 SELECT 多带 prev_odds 会 500。"""
    r = client.get(f'/api/live/{MID}')
    assert r.status_code == 200
    d = r.json()
    assert len(d['markets']) == 2          # 1x2 + Total
    assert d['n_markets'] == 2
    assert d['n_selections'] == 5


def test_live_detail_status_label(client, clean):
    """ms=31 是中场休息，不能显示成「未知(31)」。"""
    d = client.get(f'/api/live/{MID}').json()
    assert d['status_label'] == '中场休息'


@pytest.mark.parametrize('code,expect', [
    (0, '未开始'),
    (6, '进行中'),
    (7, '进行中'),
    (31, '中场休息'),
    (100, '已结束'),
    (120, '点球大战'),
])
def test_status_label_known_codes(code, expect):
    from web.api import _status_label
    assert _status_label(code) == expect


def test_status_label_unknown_code_not_scary():
    """未知码显示成「进行中 · 码 X」，不是「未知(X)」。

    状态码是平台私有编号，实测美式足球出现过 303。显示「未知」会让
    用户以为数据坏了，实际上那场比赛就是在踢。
    """
    from web.api import _status_label
    label = _status_label(303)
    assert '未知' not in label
    assert '303' in label          # 保留原值方便排查


def test_status_label_none():
    from web.api import _status_label
    assert _status_label(None) == '—'


def test_half_time_counts_as_in_play(client, clean):
    """中场休息必须出现在「只看进行中」里。

    这里曾经写死 `status IN (6,7)`，把 ms=31 漏掉了 —— 中场当然
    还算进行中。改成黑名单 `NOT IN (0,100,120)` 才对。
    """
    r = client.get('/api/live', params={'only_in_play': 'true'})
    assert r.status_code == 200
    ids = [m['match_id'] for m in r.json()['matches']]
    assert MID in ids, '中场休息的比赛被「只看进行中」过滤掉了'


def test_live_detail_404(client):
    r = client.get('/api/live/_no_such_match_')
    assert r.status_code == 404


# ---------------------------------------------------------------- 走势

def test_live_history_points(client, clean):
    r = client.get(f'/api/live/{MID}/history',
                   params={'market': '1x2', 'label': '1'})
    assert r.status_code == 200
    pts = r.json()['points']
    assert len(pts) == 3
    assert [p['odds'] for p in pts] == [1.90, 1.85, 1.80]
    # 每点带比分上下文 —— 这是滚球走势和赛前走势的区别
    assert pts[1]['minute'] == 55
    assert pts[1]['home_score'] == 1


def test_live_history_empty_for_unknown_selection(client, clean):
    r = client.get(f'/api/live/{MID}/history',
                   params={'market': '1x2', 'label': '2'})
    assert r.status_code == 200
    assert r.json()['points'] == []


# ---------------------------------------------------------------- 页面路由

@pytest.mark.parametrize('path', [
    '/live',
    f'/live/{MID}',
    f'/live/detail?id={MID}',
])
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert b'<html' in r.content[:200].lower()


# ---------------------------------------------------------------- 到期检查

def test_live_result_state_expires_stale_quote():
    """P0：当前指针没变，但报价超过 90 秒也必须判过期。"""
    from datetime import timedelta
    from web.api import _live_result_state

    stale = {'analysis_status': 'valid', 'snapshot_id': 's1',
             'odds_updated': beijing_now() - timedelta(seconds=120)}
    status, age = _live_result_state(stale, 's1')
    assert status == 'expired'
    assert age >= 120

    fresh = {'analysis_status': 'valid', 'snapshot_id': 's1',
             'verification_status': 'verified', 'cost': 0.8,
             'odds_updated': beijing_now() - timedelta(seconds=5)}
    assert _live_result_state(fresh, 's1')[0] == 'verified_candidate'
    # 新快照已发布、结果还停在上一版：分析中，不是已失效
    assert _live_result_state(fresh, 's2')[0] == 'analyzing'
    # 当前指针被清空（比分变化/空响应/删盘）：已失效
    assert _live_result_state(fresh, None)[0] == 'invalidated'


def test_invalidation_reason_uses_observed_change_only():
    from web.api import _live_invalidation_reason

    class SavedSnapshot:
        def get_live_snapshot(self, *_args):
            return {'state_after': {'ms': 6}}

    dao = SavedSnapshot()
    old = {'snapshot_id': 's1', 'home_score': 0, 'away_score': 1}
    live = {'match_id': MID, 'current_snapshot_id': None,
            'status': 6, 'home_score': 1, 'away_score': 1}
    assert '比分已从 0:1 变为 1:1' in _live_invalidation_reason(old, live, dao)
    live['home_score'] = 0
    live['status'] = 31
    assert '阶段或状态已变化' in _live_invalidation_reason(old, live, dao)
    live['status'] = 6
    assert '没有保存更具体' in _live_invalidation_reason(old, live, dao)
    live['current_snapshot_id'] = 's2'
    assert '新赔率快照已收到' in _live_invalidation_reason(old, live, dao)


def _seed_result(d, cost, analysis_status='valid',
                 verification_status=None, age_sec=5, snapshot_id='s1'):
    with d.conn.cursor() as c:
        c.execute("DELETE FROM kessbet_live_result_cost WHERE match_id=%s",
                  (MID,))
        c.execute("""INSERT INTO kessbet_live_result_cost
                     (platform, match_id, cost, profit_pct, chosen,
                      reach_states, n_candidates, minute, home_score,
                      away_score, kickoff_time, computed_time,
                      odds_updated, snapshot_id, generation,
                      verification_status, analysis_status)
                     VALUES ('kessbet',%s,%s,28.0,'[]',0,0,55,1,0,
                             NOW() - INTERVAL 1 HOUR, NOW(),
                             NOW() - INTERVAL %s SECOND,%s,1,%s,%s)""",
                  (MID, cost, age_sec, snapshot_id,
                   verification_status, analysis_status))
        c.execute("""UPDATE kessbet_match_live SET current_snapshot_id=%s
                     WHERE match_id=%s""", (snapshot_id, MID))
    d.conn.commit()


def test_result_costs_null_cost_is_json_safe(client, clean):
    _seed_result(clean, cost=None, analysis_status='no_cover')
    r = client.get('/api/live/result-costs?limit=500')
    assert r.status_code == 200
    row = [x for x in r.json()['rows'] if x['match_id'] == MID][0]
    assert row['cost'] is None
    assert row['analysis_status'] == 'no_candidate'
    assert r.json()['arbitrage'] == 0


def test_detail_shows_expired_not_arbitrage(client, clean):
    _seed_result(clean, cost=0.8, analysis_status='valid',
                 verification_status='verified', age_sec=120)
    d = client.get(f'/api/live/{MID}').json()
    assert d['result']['analysis_status'] == 'expired'
    assert d['result']['is_arbitrage'] is False
    assert d['result']['status_text'] == '已过期'
    assert d['snapshot_id'] == 's1'


# ---------------------------------------------------------------- 证据页

EVID_MID = '_test_evidence_'


def _insert_evidence_snapshot(d):
    import json
    replay = {
        'minute': 50,
        'home_score': 0,
        'away_score': 0,
        'raw_fetched_time': '2026-11-17 13:00:00',
        'raw': {
            'fetched_time': '2026-11-17 13:00:00',
            'state': {'ms': 7, 'mt': '50:00'},
            'markets': [{'mari': '1|m,1|1,2.0,1,1|1x2'}],
        },
        'expected': {
            'cost': 0.9,
            'chosen': [['1x2|||1', 2.0]],
            'n_candidates': 1,
            'is_arbitrage': True,
        },
        'selections': [['1x2', '', '', '1', 2.0]],
    }
    with d.conn.cursor() as c:
        c.execute("""INSERT INTO kessbet_arbitrage_snapshot
                     (alert_time, platform, match_id, canonical_id, sport,
                      home_team, away_team, league, kickoff_time,
                      cost, cost_scope, ilp_chosen, lp_chosen,
                      n_markets, n_selections, n_candidates,
                      market_detail, replay_context, odds_updated, is_live)
                     VALUES (NOW(),'kessbet',%s,%s,'sr:sport:1','H','A','L',
                      NOW() - INTERVAL 1 HOUR, 0.9, 'single',
                      '["1x2|||1"]',
                      '[{"desc":"1x2|||1","weight":1.0}]',
                      1, 1, 1, '[]', %s, NOW() - INTERVAL 1 MINUTE, 1)""",
                  (EVID_MID, f'local:kessbet:{EVID_MID}',
                   json.dumps(replay, ensure_ascii=False)))
        sid = c.lastrowid
    d.conn.commit()
    return sid


def _purge_evidence(d):
    with d.conn.cursor() as c:
        c.execute("DELETE FROM kessbet_arbitrage_snapshot WHERE match_id=%s",
                  (EVID_MID,))
    d.conn.commit()


def test_evidence_api_exposes_raw_payload(client):
    d = Dao()
    _purge_evidence(d)
    try:
        sid = _insert_evidence_snapshot(d)
        r = client.get(f'/api/evidence/{sid}')
        assert r.status_code == 200
        data = r.json()
        assert data['evidence']['raw_available'] is True
        assert data['evidence']['level'] == 'E1'
        assert data['replay']['raw']['markets'][0]['mari']
        assert data['lp_chosen'][0]['weight'] == 1.0
    finally:
        _purge_evidence(d)
        d.close()


def test_evidence_page_renders(client):
    d = Dao()
    _purge_evidence(d)
    try:
        sid = _insert_evidence_snapshot(d)
        r = client.get(f'/evidence/{sid}')
        assert r.status_code == 200
        assert b'<html' in r.content[:200].lower()
    finally:
        _purge_evidence(d)
        d.close()


def test_live_raw_keeps_recent_and_finds_nearest():
    d = Dao()
    mid = '_test_live_raw_'
    try:
        base = beijing_now()
        for i in range(7):
            d.save_live_raw(mid, base, f'{{"i":{i}}}')
        with d.conn.cursor() as c:
            c.execute("SELECT COUNT(*) FROM kessbet_odds_live_raw "
                      "WHERE match_id=%s", (mid,))
            assert c.fetchone()[0] == 5
        row = d.get_live_raw_near(mid, base)
        assert row is not None and row['payload']
    finally:
        with d.conn.cursor() as c:
            c.execute("DELETE FROM kessbet_odds_live_raw WHERE match_id=%s",
                      (mid,))
        d.conn.commit()
        d.close()


def test_solver_market_status_layers():
    """页面分层：参与求解 / 非结果盘 / 半场盘 / 非白名单 / 缺腿。"""
    from web.api import _solver_market_status

    def m(base, side, sels):
        return {'base': base, 'side': side,
                'selections': [{'line': ln, 'label': lab}
                               for ln, lab in sels]}

    assert _solver_market_status(
        m('1x2', '', [('', '1'), ('', 'X'), ('', '2')])) == (
            True, 'in_solver')
    assert _solver_market_status(
        m('1x2', '', [('', 'X'), ('', '2')])) == (False, 'incomplete')
    assert _solver_market_status(
        m('5th Goal', '', [('', '1')])) == (
            False, 'not_result_market')
    assert _solver_market_status(
        m('1st Half - 1x2', '', [('', '1'), ('', 'X'), ('', '2')])) == (
            False, 'half_market')
    assert _solver_market_status(
        m('Correct Score', '', [('', 'other')])) == (
            False, 'not_whitelisted')
    assert _solver_market_status(
        m('Correct Score', '', [('', '3:0')])) == (True, 'in_solver')
