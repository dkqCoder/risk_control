"""滚球列表接口（ACTION=00232）的测试。

为什么这组测试重要：
  赛程接口(00252)不返回已开赛的比赛，滚球列表是唯一能发现
  「赛前漏采但正在踢」的比赛的途径。它挂了不会报错，只会静默漏采 ——
  所以要有测试盯着。

不依赖网络：mock 掉 _post。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest

from collect.kessbet import http as H

# 真实抓包得到的响应片段（2026-11-12，足球滚球 3 场里取 2 场）
RAW_MATCH = {
    'matchId': '245167973185130840',
    'htn': 'Deportes Quindio',
    'atn': 'Tigres FC',
    'ln': 'Torneo DIMAYOR',
    'cn': 'Colombia',
    'tmi': 'sr:match:76409632',
    'tsi': 'sr:sport:1',
    'mbtp': 1762909800000,          # 2026-11-12 09:10 北京时间
    'ms': '7',
    'mt': '62:12',
    'hmp': '1',
    'amp': '0',
    'mari': '1,245168819293687974/1|245167973185130840,1|245168819293687976,'
            '1.12,1,1^245168819293687977,5.30,1,X|1x2',
}


@pytest.fixture()
def client(monkeypatch):
    """构造一个不发网络请求的 KessbetHttp。"""
    c = H.KessbetHttp.__new__(H.KessbetHttp)
    c.session = None
    c.timeout = 1
    c.calls = []

    def fake_post(action, body):
        c.calls.append((action, body))
        return {'matchList': [dict(RAW_MATCH)], 'leagueGroupList': []}

    c._post = fake_post
    return c


# ---------------------------------------------------------------- 接口

def test_live_list_uses_00232(client):
    """必须用 00232 —— 00252 是赛程接口，不返回滚球。"""
    client.live_list_raw(1)
    action, body = client.calls[0]
    assert action == '00232'
    assert action != H.ACTION_LIST


def test_live_list_body_shape(client):
    """请求体要和网页实际发的一致（抓包验证过的参数）。"""
    client.live_list_raw(1)
    _, body = client.calls[0]
    assert body == {'tabId': '1', 'playType': '1',
                    'sortBy': 6, 'specifiers': ''}


def test_live_list_handles_odd_response(client, monkeypatch):
    """平台偶尔返回非 dict —— 不能抛，要返回空列表。"""
    client._post = lambda a, b: None
    assert client.live_list_raw(1) == []

    client._post = lambda a, b: {'matchList': None}
    assert client.live_list_raw(1) == []


def test_list_live_matches_converts_to_model(client):
    ms = client.list_live_matches('sr:sport:1')
    assert len(ms) == 1
    m = ms[0]
    assert m.match_id == '245167973185130840'
    assert m.home_team == 'Deportes Quindio'
    assert m.away_team == 'Tigres FC'
    assert m.sport == 'sr:sport:1'
    assert m.sportradar_id == 'sr:match:76409632'


def test_list_live_matches_skips_bad_records(client):
    """缺 mbtp 的记录要跳过，不能让整批失败。"""
    client._post = lambda a, b: {'matchList': [
        {'matchId': 'x'},                    # 没有 mbtp
        dict(RAW_MATCH),
    ]}
    ms = client.list_live_matches('sr:sport:1')
    assert [m.match_id for m in ms] == ['245167973185130840']


# ---------------------------------------------------------------- 采集侧

def test_refresh_live_schedule_only_inserts_unknown(monkeypatch):
    """已经库里的比赛不该反复写 —— 每 60 秒一次，写 4000 行没必要。"""
    from collect import run as R

    class FakeAdapter:
        def list_live_matches(self, sport):
            from common.models import Match
            from common.timeutil import now_beijing
            return [Match(platform='kessbet', match_id=mid,
                          sport='sr:sport:1', league='L', country='C',
                          home_team='H', away_team='A',
                          kickoff_time=now_beijing(), sportradar_id='')
                    for mid in ('known', 'new')]

    class FakeDao:
        def known_match_ids(self, platform='kessbet'):
            return {'known'}

        def upsert_matches(self, matches):
            self.upserted = [m.match_id for m in matches]
            return len(matches)

        def sync_canonical(self, matches):
            pass

    c = R.Collector.__new__(R.Collector)
    c.adapter = FakeAdapter()
    c.dao = FakeDao()
    c.stats = {}
    monkeypatch.setattr(R.config, 'LIVE_ENABLED', True)
    monkeypatch.setattr(R.config, 'LIVE_SPORTS', ('sr:sport:1',))

    added = c.refresh_live_schedule()
    assert added == 1
    assert c.dao.upserted == ['new']
