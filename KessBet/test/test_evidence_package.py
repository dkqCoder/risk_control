"""Task 6：E1 证据包写入与正式命中门槛。"""
import hashlib
import json
import os
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from common.evidence import write_evidence_package
from common.timeutil import now_beijing


def _snapshot():
    return {
        'snapshot_id': 's1', 'match_id': 'm1', 'generation': 1,
        'request_json': {
            'url': 'https://api.example/handler?token=abc',
            'headers': {'Authorization': 'Bearer secret',
                        'content-type': 'application/json'},
            'body': {'matchId': 'm1'},
        },
        'response_json': {
            'status': 'ok_nonempty', 'response_status': 200,
            'response_headers': {'content-type': 'application/json'},
            'raw_body': '{"code":1,"data":[]}',
            'markets': [{'mari': '4|m,1|2.5,id/1=id2,1.9,1,over|Total'}],
        },
        'raw_payload': [{'mari': '4|m,1|2.5,id/1=id2,1.9,1,over|Total'}],
        'rows_json': [['Total', '', '2.5', 'over', 1.9]],
        'response_hash': 'a' * 64,
        'state_before': {'ms': 7}, 'state_after': {'ms': 7},
        'minute': 50, 'home_score': 0, 'away_score': 0,
        'ht_home': None, 'ht_away': None,
        'phase': 'second_half', 'phase_source': 'inferred',
        'ht_source': 'unknown',
        'input_fingerprint': 'f' * 64, 'parser_version': 'p1',
        'filter_policy': 'drop_broken=False',
        'request_started_at': '2026-11-17 10:00:00',
        'response_received_at': '2026-11-17 10:00:01',
    }


def test_package_has_manifest_and_hashes(tmp_path):
    m = write_evidence_package(
        str(tmp_path), _snapshot(),
        {'analysis_id': 's1', 'analyzer_version': 'v3',
         'expected': {'cost': 0.9}},
        {'status': 'verified', 'min_payout': 10100})
    for name, meta in m['files'].items():
        p = tmp_path / name
        assert p.exists()
        assert hashlib.sha256(p.read_bytes()).hexdigest() == meta['sha256']
    assert (tmp_path / 'manifest.json').exists()


def test_package_omits_secrets(tmp_path):
    write_evidence_package(
        str(tmp_path), _snapshot(),
        {'analysis_id': 's1'}, {'status': 'verified'})
    req = (tmp_path / 'request.json').read_text(encoding='utf-8')
    assert 'Authorization' not in req
    assert 'Bearer secret' not in req
    assert 'token=***' in req


def test_package_is_offline_readable(tmp_path):
    write_evidence_package(
        str(tmp_path), _snapshot(),
        {'analysis_id': 's1', 'expected': {'cost': 0.9}},
        {'status': 'verified', 'min_payout': 10100})
    norm = json.loads((tmp_path / 'normalized.json').read_text(
        encoding='utf-8'))
    assert norm['selections'][0][0] == 'Total'
    manifest = json.loads((tmp_path / 'manifest.json').read_text(
        encoding='utf-8'))
    assert manifest['input_fingerprint'] == 'f' * 64


class _FakeDao:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.kw = None

    def has_arbitrage_fingerprint(self, *a, **k):
        return False

    def get_live_snapshot(self, match_id, snapshot_id):
        return self.snapshot

    def write_arbitrage_snapshot(self, **kw):
        self.kw = kw
        return 42


def test_persist_hit_requires_complete_package(monkeypatch, tmp_path):
    from analyze.live import run as L
    from collect import config

    monkeypatch.setattr(config, 'LIVE_EVIDENCE_DIR', str(tmp_path))
    d = _FakeDao(_snapshot())
    meta = {
        'match_id': 'm1', 'sport': 'sr:sport:1',
        'home_team': 'H', 'away_team': 'A', 'league': 'L',
        'kickoff_time': datetime(2026, 11, 17, 9, 0),
        'snapshot_id': 's1', 'input_fingerprint': 'f' * 64,
        'minute': 50, 'home_score': 0, 'away_score': 0,
        'ht_home': None, 'ht_away': None,
        'match_status': 7, 'match_clock': '50:00',
        'phase': 'second_half', 'phase_source': 'inferred',
        'ht_source': 'unknown',
        'odds_updated': now_beijing(),
        'snapshot_selections': [['Total', '', '2.5', 'over', 1.9]],
    }
    r = {
        'chosen': [('1x2|||1', 3.2), ('1x2|||X', 3.2),
                   ('1x2|||2', 3.2)],
        'cost': 0.9375, 'reach': 2025, 'reach_mask': 0,
        'n_candidates': 3, 'is_arbitrage': True, 'is_candidate': True,
        'profit_pct': 6.67,
        'verification': {'status': 'verified', 'min_payout': 10666.0},
    }
    got = L._persist_hit(d, meta, r, now_beijing())
    assert got == 42
    assert d.kw['evidence_status'] == 'complete'
    assert d.kw['evidence_id'] == 's1'
    assert d.kw['is_shadow'] == 0
    assert os.path.isfile(os.path.join(
        d.kw['evidence_path'], 'manifest.json'))

    d2 = _FakeDao(_snapshot())
    got2 = L._persist_hit(d2, meta, r, now_beijing(), shadow=True)
    assert got2 == 42
    assert d2.kw['is_shadow'] == 1
