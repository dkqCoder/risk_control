"""v1 验收样例（设计 §10 的纯函数/证据部分）。

数据库/浏览器联调样例由 test_live_collector、test_live_page 覆盖。
"""
import hashlib
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from analyze.live import result as R
from analyze.live import run as L
from analyze.live.verify import verify_chosen
from common.evidence import write_evidence_package
from common.fingerprint import input_fingerprint
from common.phase import classify_phase, ht_source
from collect.kessbet.http import KessbetHttp
from web.api import _live_result_state, _STATUS_TEXT


def _sels(rows):
    return [(m, s, l, lab, o) for m, s, l, lab, o in rows]


# 1. 9:0 / 7:0 反例
def test_9_0_guard_and_7_0_counterexample():
    high = _sels([
        ('Total', '', '9.5', 'over', 1.95),
        ('Total', '', '9.5', 'under', 1.65),
        ('Total', 'A', '0.5', 'over', 5.70),
        ('Total', 'A', '0.5', 'under', 1.05),
    ])
    assert R.check_match(high, 9, 0, minute=86,
                         ht_home=2, ht_away=0) is None
    mid = _sels([
        ('Total', '', '9.5', 'under', 1.65),
        ('Total', '', '9.5', 'over', 2.00),
        ('Total', 'A', '0.5', 'over', 5.70),
        ('Total', 'A', '0.5', 'under', 1.10),
    ])
    r = R.check_match(mid, 7, 0, minute=85, ht_home=3, ht_away=0)
    assert r is not None and r['is_arbitrage'] is False
    assert r['verification']['status'] == 'counterexample_found'


# 2. Correct Score other 隔离
def test_correct_score_other_not_certified():
    sels = _sels([
        ('Correct Score', '', '', '3:0', 1.5),
        ('Correct Score', '', '', '4:0', 5.0),
        ('Correct Score', '', '', 'other', 40.0),
        ('Total', '', '3.5', 'over', 3.25),
    ])
    assert all(not d.endswith('|other')
               for _, _, d in R.build_candidates(sels, minute=84))


# 3. 正向白名单
def test_positive_whitelist_combo_verifies():
    r = R.check_match(_sels([
        ('1x2', '', '', '1', 3.2),
        ('1x2', '', '', 'X', 3.2),
        ('1x2', '', '', '2', 3.2),
    ]), 0, 0, minute=10)
    assert r['is_arbitrage'] is True
    assert r['verification']['status'] == 'verified'


# 4. 阶段/半场保守
def test_phase_and_half_time_are_conservative():
    assert classify_phase({'ms': 7, 'mt': '60:00'})[1] == 'inferred'
    assert ht_source('second_half') == 'inferred_history'
    assert L._effective_ht({'ht_home': 1, 'ht_away': 0,
                            'ht_source': 'inferred_history'}) == (None, None)
    half = _sels([('1st Half - 1x2', '', '', '1', 2.0),
                  ('2nd Half - 1x2', '', '', '1', 2.0)])
    assert R.build_candidates(half, minute=20) == []


# 5. HTTP 四类结果
def test_fetch_result_classes(monkeypatch):
    def wrap(data, ok=True, kind=None):
        return {'ok': ok, 'data': data, 'raw_body': 'x',
                'response_status': 200, 'request': {},
                'error': None if ok else 'e', 'error_kind': kind,
                'received_at': None}
    monkeypatch.setattr(KessbetHttp, '_post_result',
                        lambda *a, **k: wrap(None, False, 'transport'))
    assert KessbetHttp().fetch_raw_result('m')['status'] == 'transport_error'
    monkeypatch.setattr(KessbetHttp, '_post_result',
                        lambda *a, **k: wrap([]))
    assert KessbetHttp().fetch_raw_result('m')['status'] == 'ok_empty'
    monkeypatch.setattr(KessbetHttp, '_post_result',
                        lambda *a, **k: wrap({'bad': 1}))
    assert KessbetHttp().fetch_raw_result('m')['status'] == 'protocol_error'


# 6. 指纹绑定：比分变化 -> 新输入
def test_fingerprint_binds_score_and_selection():
    s = [{'mari': 'm', 'market': '1x2', 'side': '', 'line': '',
          'label': '1', 'odds': 2.0, 'market_id': '1', 'locked': False}]
    a = input_fingerprint(s, 0, 0, 'first_half', None, 'p', 'r')
    assert a == input_fingerprint(s, 0, 0, 'first_half', None, 'p', 'r')
    assert a != input_fingerprint(s, 1, 0, 'first_half', None, 'p', 'r')


# 7/10. 状态映射与过期
def test_state_mapping_and_expiry():
    from datetime import timedelta
    from common.timeutil import now_beijing
    fresh = {'analysis_status': 'valid', 'snapshot_id': 's1',
             'verification_status': 'verified', 'cost': 0.8,
             'odds_updated': now_beijing() - timedelta(seconds=5)}
    assert _live_result_state(fresh, 's1')[0] == 'verified_candidate'
    stale = dict(fresh, odds_updated=now_beijing() - timedelta(seconds=120))
    assert _live_result_state(stale, 's1')[0] == 'expired'
    assert _live_result_state(fresh, 's2')[0] == 'analyzing'
    assert _live_result_state(fresh, None)[0] == 'invalidated'
    assert _STATUS_TEXT['shadow_candidate'] == '试算候选（未发布）'


# 8/9. 证据包与离线读取
def test_evidence_package_offline(tmp_path):
    snap = {
        'snapshot_id': 's1', 'match_id': 'm1',
        'request_json': {'headers': {'Authorization': 'x'}}, 'rows_json': [],
        'raw_payload': [], 'state_after': {'ms': 7},
        'input_fingerprint': 'f' * 64,
    }
    m = write_evidence_package(
        str(tmp_path), snap,
        {'analysis_id': 's1', 'expected': {'cost': 0.9}},
        {'status': 'verified'})
    for name, meta in m['files'].items():
        p = tmp_path / name
        assert hashlib.sha256(p.read_bytes()).hexdigest() == meta['sha256']
    assert 'Authorization' not in (tmp_path / 'request.json').read_text(
        encoding='utf-8')
    assert json.loads((tmp_path / 'analysis.json').read_text(
        encoding='utf-8'))['analysis_id'] == 's1'
