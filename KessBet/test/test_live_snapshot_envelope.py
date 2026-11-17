"""Task 2：输入指纹与快照信封。"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest

from common.fingerprint import input_fingerprint


def _sel(odds=2.5, mari='1|m,1|1,2.5,1,1|1x2', label='1', locked=False):
    return {'mari': mari, 'market': '1x2', 'side': '', 'line': '',
            'label': label, 'odds': odds, 'market_id': '1',
            'locked': locked}


def test_fingerprint_ignores_receive_time_and_order():
    a = input_fingerprint([_sel(), _sel(label='X', odds=3.2)],
                          1, 0, 'second_half', (1, 0), 'p', 'r1')
    b = input_fingerprint([_sel(label='X', odds=3.2), _sel()],
                          1, 0, 'second_half', (1, 0), 'p', 'r1')
    assert a == b and len(a) == 64


def test_fingerprint_changes_with_odds_phase_and_locked():
    base = input_fingerprint([_sel()], 1, 0, 'second_half', (1, 0), 'p', 'r1')
    assert base != input_fingerprint([_sel(odds=2.6)], 1, 0,
                                     'second_half', (1, 0), 'p', 'r1')
    assert base != input_fingerprint([_sel()], 1, 0,
                                     'first_half', (1, 0), 'p', 'r1')
    assert base != input_fingerprint([_sel(locked=True)], 1, 0,
                                     'second_half', (1, 0), 'p', 'r1')
    assert base != input_fingerprint([_sel(mari='1|m,1|1,2.5,0,1|1x2')],
                                     1, 0, 'second_half', (1, 0), 'p', 'r1')
