"""套利命中弹窗：滚球命中要能拿到命中时刻的比分和比赛时间。

这些值存在 kessbet_arbitrage_snapshot.replay_context 里（滚球命中才有），
接口必须把它们带出来，弹窗才能显示「1:1 · 56' · 下半场」。
"""
import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from web.api import _arb_row  # noqa: E402


def _row(**over):
    r = {
        'id': 744,
        'alert_time': datetime(2026, 11, 21, 21, 15),
        'match_id': '245131530387624840',
        'canonical_id': 'local:kessbet:245131530387624840',
        'sport': 'sr:sport:1',
        'home_team': 'Hannover 96',
        'away_team': 'Greuther Furth',
        'league': '2. Bundesliga',
        'kickoff_time': datetime(2026, 11, 21, 20, 0),
        'cost': 0.6943866943866943,
        'cost_scope': 'single',
        'lp_worst_profit': None,
        'cross_gap': None,
        'n_markets': 2,
        'n_selections': 2,
        'n_candidates': 76,
        'ilp_chosen': json.dumps(['1x2|||2']),
        'lp_chosen': json.dumps([{'desc': '1x2|||2', 'weight': 0.5}]),
        'odds_updated': datetime(2026, 11, 21, 21, 15),
        'is_live': 1,
        'is_shadow': 1,
        'replay_context': None,
    }
    r.update(over)
    return _arb_row(r)


def test_live_hit_exposes_score_and_match_time():
    ctx = json.dumps({
        'minute': 56,
        'home_score': 1,
        'away_score': 1,
        'match_clock': '56:09',
        'phase': 'second_half',
    })
    out = _row(replay_context=ctx)
    assert out['score'] == '1:1'
    assert out['minute'] == 56
    assert out['match_clock'] == '56:09'
    assert out['phase'] == 'second_half'


def test_prematch_hit_has_no_live_score():
    out = _row(is_live=0, replay_context=None)
    assert out['score'] is None
    assert out['minute'] is None
    assert out['match_clock'] is None


def test_broken_replay_context_does_not_crash():
    out = _row(replay_context='not-json')
    assert out['score'] is None
