"""Task 3：阶段/半场来源分级。"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from common.phase import classify_phase, ht_source
from analyze.live.run import _effective_ht


def test_phase_uses_status_and_clock():
    assert classify_phase({'ms': 31, 'mt': '45:00'}) == (
        'halftime', 'official_state')
    assert classify_phase({'ms': 100}) == ('finished', 'official_state')
    assert classify_phase({'ms': 7}) == ('unknown', 'inferred')
    phase, source = classify_phase({'ms': 7, 'mt': '60:00'})
    assert phase == 'second_half'
    assert source == 'inferred'      # 不能冒充权威阶段


def test_first_half_clock_is_inferred():
    assert classify_phase({'ms': 7, 'mt': '30:00'}) == (
        'first_half', 'inferred')


def test_ht_source_uses_official_halftime_checkpoint():
    assert ht_source('first_half') == 'unknown'
    assert ht_source('halftime') == 'official_state'
    assert ht_source('second_half') == 'inferred_history'
    assert ht_source('unknown') == 'unknown'


def test_effective_ht_only_for_official_source():
    snap = {'ht_home': 1, 'ht_away': 0, 'ht_source': 'inferred_history'}
    assert _effective_ht(snap) == (None, None)
    snap['ht_source'] = 'official_state'
    assert _effective_ht(snap) == (1, 0)
    assert _effective_ht({}) == (None, None)
