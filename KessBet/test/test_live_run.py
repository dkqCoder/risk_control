"""analyze/live/run.py 的即时快照与命中留证测试。"""
from datetime import datetime

from analyze.live import run as L


def _meta(**overrides):
    meta = {
        'match_id': 'm1',
        'sport': 'sr:sport:1',
        'home_team': 'Home',
        'away_team': 'Away',
        'league': 'League',
        'kickoff_time': datetime(2026, 11, 12, 12, 0),
        'minute': 30,
        'home_score': 0,
        'away_score': 0,
        'odds_updated': datetime(2026, 11, 12, 12, 30),
        'snapshot_selections': [],
    }
    meta.update(overrides)
    return meta


def _arbitrage_result():
    return {
        'is_arbitrage': True,
        'cost': 0.9,
        'profit_pct': 11.11,
        'chosen': [('1x2||1', 2.2), ('1x2||X', 4.0), ('1x2||2', 5.2)],
        'n_candidates': 3,
        'reach': 9,
    }


def test_snapshot_skips_non_football(monkeypatch):
    monkeypatch.setattr(L.config, 'LIVE_ANALYZE_ENABLED', True)
    called = []
    monkeypatch.setattr(L.result, 'check_match', lambda *a, **kw: called.append(True))

    out = L.analyze_live_snapshot(object(), _meta(sport='sr:sport:2'), [])

    assert out['error'] == 'unsupported_sport'
    assert called == []


def test_snapshot_respects_analyze_switch(monkeypatch):
    """判定总开关必须同时挡住采集侧的快照回调。

    `LIVE_ANALYZE_ENABLED` 只停掉分析线程是不够的 —— 采集器每收到
    一次推送仍会调 analyze_live_snapshot，不挡的话"关掉判定"只关一半。
    """
    monkeypatch.setattr(L.config, 'LIVE_ANALYZE_ENABLED', False)
    called = []
    monkeypatch.setattr(L.result, 'check_match', lambda *a, **kw: called.append(True))

    out = L.analyze_live_snapshot(object(), _meta(), [])

    assert out['error'] == 'analyze_disabled'
    assert called == []


def test_snapshot_shoots_only_for_new_hit(monkeypatch):
    monkeypatch.setattr(L.config, 'LIVE_ANALYZE_ENABLED', True)
    shots = []
    monkeypatch.setattr(L.result, 'check_match', lambda *a, **kw: _arbitrage_result())
    monkeypatch.setattr(L, '_persist_cost', lambda *a, **kw: None)
    monkeypatch.setattr(L, '_persist_hit', lambda *a, **kw: True)
    monkeypatch.setattr(L, '_shoot_async', lambda *a, **kw: shots.append(True))

    L.analyze_live_snapshot(object(), _meta(), [])

    assert shots == [True]


def test_snapshot_does_not_shoot_duplicate_hit(monkeypatch):
    monkeypatch.setattr(L.config, 'LIVE_ANALYZE_ENABLED', True)
    shots = []
    monkeypatch.setattr(L.result, 'check_match', lambda *a, **kw: _arbitrage_result())
    monkeypatch.setattr(L, '_persist_cost', lambda *a, **kw: None)
    monkeypatch.setattr(L, '_persist_hit', lambda *a, **kw: False)
    monkeypatch.setattr(L, '_shoot_async', lambda *a, **kw: shots.append(True))

    L.analyze_live_snapshot(object(), _meta(), [])

    assert shots == []


def test_fallback_does_not_shoot_when_hits_not_written(monkeypatch):
    class Dao:
        def live_analyze_pending(self, _window):
            return [_meta()]

    shots = []
    monkeypatch.setattr(
        L, 'evaluate_live_match',
        lambda *a, **kw: {'result': _arbitrage_result(), 'error': None,
                          'ht': None, 'selections': [], 'is_stale': False},
    )
    monkeypatch.setattr(L, '_shoot_async', lambda *a, **kw: shots.append(True))

    stats = L.analyze_live_once(Dao(), write_hits=False, write_costs=False)

    assert stats['hits'] == 1
    assert shots == []


def test_replay_live_evidence_reproduces_saved_result():
    selections = [
        ('1x2', '', '', '1', 3.5),
        ('1x2', '', '', 'X', 3.5),
        ('1x2', '', '', '2', 3.5),
    ]
    result = L.result.check_match(selections, 0, 0, minute=10)
    context = {
        'schema_version': 1,
        'analyzer_version': L.LIVE_ANALYZER_VERSION,
        'minute': 10,
        'home_score': 0,
        'away_score': 0,
        'ht_home': None,
        'ht_away': None,
        'first_half_live': True,
        'reach_mask_hex': hex(result['reach_mask']),
        'expected': {
            'cost': result['cost'],
            'chosen': result['chosen'],
            'n_candidates': result['n_candidates'],
            'is_arbitrage': result['is_arbitrage'],
        },
        'selections': [list(x) for x in selections],
    }

    replay = L.replay_live_evidence(context)

    assert replay['ok'] is True
    assert replay['exact_match'] is True
    assert all(replay['checks'].values())


def test_replay_accepts_equivalent_alternative_optimum(monkeypatch):
    context = {
        'minute': 60, 'home_score': 1, 'away_score': 0,
        'ht_home': 1, 'ht_away': 0, 'first_half_live': False,
        'reach_mask_hex': '0x7',
        'expected': {
            'cost': 0.9, 'chosen': [['original', 2.0]],
            'n_candidates': 3, 'is_arbitrage': True,
        },
        'selections': [],
    }
    monkeypatch.setattr(L.result, 'check_match', lambda *a, **kw: {
        'cost': 0.9, 'chosen': [('alternative', 2.0)],
        'n_candidates': 3, 'is_arbitrage': True,
        'reach_mask': 0x7,
    })

    replay = L.replay_live_evidence(context)

    assert replay['ok'] is True
    assert replay['exact_match'] is False
    assert replay['alternative_optimum'] is True
