"""Task 7：影子模式不写正式命中。"""
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from analyze.live import run as L
from common.timeutil import now_beijing


def _meta():
    return {
        'match_id': 'm1', 'sport': 'sr:sport:1',
        'home_team': 'H', 'away_team': 'A', 'league': 'L',
        'kickoff_time': datetime(2026, 11, 17, 9, 0),
        'minute': 50, 'home_score': 0, 'away_score': 0,
        'ht_home': None, 'ht_away': None,
        'match_status': 7, 'match_clock': '50:00',
        'phase': 'second_half', 'phase_source': 'inferred',
        'ht_source': 'unknown', 'snapshot_id': 's1',
        'current_snapshot_id': 's1', 'generation': 1,
        'input_fingerprint': 'f' * 64,
        'odds_updated': now_beijing(),
        'snapshot_selections': [],
    }


def _result():
    return {
        'is_arbitrage': True, 'is_candidate': True,
        'cost': 0.9, 'profit_pct': 11.11,
        'chosen': [('1x2|||1', 2.2), ('1x2|||X', 4.0),
                   ('1x2|||2', 5.2)],
        'n_candidates': 3, 'reach': 9, 'reach_mask': 0,
        'verification': {'status': 'verified'},
    }


class _Dao:
    def __init__(self):
        self.cost_kw = None

    def live_analyze_pending(self, _window):
        return [_meta()]

    def write_live_result_cost(self, **kw):
        self.cost_kw = kw


def test_shadow_computes_but_does_not_write_hit(monkeypatch):
    monkeypatch.setattr(L.config, 'LIVE_ANALYZE_ENABLED', True)
    monkeypatch.setattr(L.config, 'LIVE_SHADOW_MODE', True)
    monkeypatch.setattr(L, 'evaluate_live_match', lambda *a, **k: {
        'result': _result(), 'error': None, 'ht': None,
        'ht_home': None, 'ht_away': None, 'selections': [],
        'is_stale': False, 'minute': 50, 'home_score': 0,
        'away_score': 0, 'match_status': 7, 'match_clock': '50:00',
        'phase': 'second_half', 'phase_source': 'inferred',
        'ht_source': 'unknown', 'snapshot_id': 's1', 'generation': 1,
        'input_fingerprint': 'f' * 64, 'odds_updated': now_beijing(),
    })
    hits, evidence = [], []
    monkeypatch.setattr(L, '_persist_hit',
                        lambda *a, **k: hits.append(1))
    monkeypatch.setattr(L, '_write_evidence',
                        lambda *a, **k: evidence.append(1) or {
                            'evidence_status': 'complete'})

    d = _Dao()
    L.analyze_live_once(d)

    assert hits == [], '影子模式不能写正式命中'
    assert evidence == [1]
    assert d.cost_kw['analysis_status'] == 'shadow_candidate'


def test_formal_mode_writes_hit(monkeypatch):
    monkeypatch.setattr(L.config, 'LIVE_ANALYZE_ENABLED', True)
    monkeypatch.setattr(L.config, 'LIVE_SHADOW_MODE', False)
    monkeypatch.setattr(L, 'evaluate_live_match', lambda *a, **k: {
        'result': _result(), 'error': None, 'ht': None,
        'ht_home': None, 'ht_away': None, 'selections': [],
        'is_stale': False, 'minute': 50, 'home_score': 0,
        'away_score': 0, 'match_status': 7, 'match_clock': '50:00',
        'phase': 'second_half', 'phase_source': 'inferred',
        'ht_source': 'unknown', 'snapshot_id': 's1', 'generation': 1,
        'input_fingerprint': 'f' * 64, 'odds_updated': now_beijing(),
    })
    hits = []
    monkeypatch.setattr(L, '_persist_hit',
                        lambda *a, **k: hits.append(1) or 1)
    monkeypatch.setattr(L, '_shoot_async', lambda *a, **k: None)

    L.analyze_live_once(_Dao())

    assert hits == [1]


def test_shadow_snapshot_callback_does_not_write_hit(monkeypatch):
    monkeypatch.setattr(L.config, 'LIVE_ANALYZE_ENABLED', True)
    monkeypatch.setattr(L.config, 'LIVE_SHADOW_MODE', True)
    monkeypatch.setattr(L.result, 'check_match',
                        lambda *a, **k: _result())
    hits, evidence = [], []
    monkeypatch.setattr(L, '_persist_hit',
                        lambda *a, **k: hits.append(1))
    monkeypatch.setattr(L, '_write_evidence',
                        lambda *a, **k: evidence.append(1) or {
                            'evidence_status': 'complete'})
    monkeypatch.setattr(L, '_persist_cost', lambda *a, **k: None)

    L.analyze_live_snapshot(_Dao(), _meta(), [])

    assert hits == []
    assert evidence == [1]
