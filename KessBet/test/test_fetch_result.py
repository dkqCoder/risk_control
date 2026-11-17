"""F03/SDD Task 1：HTTP 结果结构化分类。"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from common.timeutil import now_beijing
from collect.kessbet.http import (ACTION_MATCH, KessbetHttp, ForbiddenError)

VALID_MARI = '4|m,1|3.5,id1/1=id2,1.75,1,over@id3,1.80,1,under|Total'


def _wrap(data, ok=True, kind=None, error=None):
    return {
        'ok': ok, 'data': data, 'raw_body': '{"code":1}',
        'response_status': 200, 'response_headers': {},
        'request': {'action': ACTION_MATCH, 'body': {'matchId': 'm1'}},
        'error': error, 'error_kind': kind,
        'platform_ts': None, 'received_at': now_beijing(),
    }


def test_fetch_result_classifies_transport_error(monkeypatch):
    monkeypatch.setattr(KessbetHttp, '_post_result',
                        lambda *a, **k: _wrap(None, ok=False,
                                              kind='transport',
                                              error='timeout'))
    r = KessbetHttp().fetch_raw_result('m1')
    assert r['status'] == 'transport_error'
    assert r['markets'] == [] and r['error']


def test_fetch_result_forbidden_is_transport_not_empty(monkeypatch):
    monkeypatch.setattr(KessbetHttp, '_post_result',
                        lambda *a, **k: _wrap(None, ok=False,
                                              kind='forbidden',
                                              error='403'))
    r = KessbetHttp().fetch_raw_result('m1')
    assert r['status'] == 'transport_error'


def test_fetch_result_ok_empty_vs_nonempty(monkeypatch):
    monkeypatch.setattr(KessbetHttp, '_post_result',
                        lambda *a, **k: _wrap([]))
    assert KessbetHttp().fetch_raw_result('m1')['status'] == 'ok_empty'

    monkeypatch.setattr(KessbetHttp, '_post_result',
                        lambda *a, **k: _wrap([{'mari': VALID_MARI}]))
    assert KessbetHttp().fetch_raw_result('m1')['status'] == 'ok_nonempty'


def test_fetch_result_protocol_error(monkeypatch):
    monkeypatch.setattr(KessbetHttp, '_post_result',
                        lambda *a, **k: _wrap({'bad': 1}))
    assert KessbetHttp().fetch_raw_result('m1')['status'] == 'protocol_error'


def test_fetch_result_parsed_empty(monkeypatch):
    monkeypatch.setattr(KessbetHttp, '_post_result',
                        lambda *a, **k: _wrap([{'mari': 'garbage'}]))
    assert KessbetHttp().fetch_raw_result('m1')['status'] == 'parsed_empty'


class _Resp:
    def __init__(self, status=200, text='{"code":1,"data":[]}',
                 body=None):
        self.status_code = status
        self.text = text
        self.headers = {'content-type': 'application/json'}
        self._body = body if body is not None else {'code': 1, 'data': []}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')

    def json(self):
        return self._body


class _Session:
    def __init__(self, resp):
        self.resp = resp

    def post(self, *a, **k):
        return self.resp


def test_post_result_preserves_raw_body_and_status():
    h = KessbetHttp()
    h.session = _Session(_Resp(text='{"code":1,"data":[]}'))
    r = h._post_result(ACTION_MATCH, {'matchId': 'm1'})
    assert r['ok'] is True
    assert r['response_status'] == 200
    assert r['raw_body'] == '{"code":1,"data":[]}'
    assert r['request']['body']['matchId'] == 'm1'
    assert r['received_at'] is not None


def test_post_result_403_is_forbidden():
    h = KessbetHttp()
    h.session = _Session(_Resp(status=403, text='denied'))
    r = h._post_result(ACTION_MATCH, {'matchId': 'm1'})
    assert r['ok'] is False
    assert r['error_kind'] == 'forbidden'
    assert r['raw_body'] == 'denied'


def test_post_raises_forbidden_for_legacy_callers():
    h = KessbetHttp()
    h.session = _Session(_Resp(status=403, text='denied'))
    try:
        h._post(ACTION_MATCH, {'matchId': 'm1'})
    except ForbiddenError:
        pass
    else:
        raise AssertionError('expected ForbiddenError')
