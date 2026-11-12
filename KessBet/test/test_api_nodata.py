"""web/api.py 单场详情的无赔率场景。

背景：比赛在赛程里但平台还没开盘时，`prematch` 没有行。
原先接口直接 `raise HTTPException(404, 'no odds for this match')`，
详情页只能显示「加载失败：HTTP 404」——用户看不出是没开盘还是链接错了。

契约：
  - 比赛存在但没有赔率   -> 200，status='nodata'，markets 为空
  - 比赛 id 根本不存在   -> 仍 404（这是真的找不到）
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from fastapi.testclient import TestClient

from web.api import app

BEIJING = timezone(timedelta(hours=8))
PREFIX = '__TEST_NOODDS__'


def _insert_match(match_id, kickoff):
    from db.dao import Dao
    dao = Dao()
    try:
        with dao.conn.cursor() as c:
            c.execute("""INSERT INTO kessbet_match
                         (platform, match_id, sport, league, country,
                          home_team, away_team, kickoff_time,
                          first_seen_time, updated_time)
                         VALUES ('kessbet',%s,'sr:sport:1','L','C','Home','Away',%s,%s,%s)
                         ON DUPLICATE KEY UPDATE kickoff_time=VALUES(kickoff_time)""",
                      (match_id, kickoff, kickoff, kickoff))
        dao.conn.commit()
    finally:
        dao.close()


def _purge():
    from db.dao import Dao
    dao = Dao()
    try:
        with dao.conn.cursor() as c:
            for t in ('kessbet_odds_prematch', 'kessbet_odds_history',
                      'kessbet_match_cost', 'kessbet_match'):
                c.execute(f"DELETE FROM {t} WHERE platform='kessbet' AND match_id LIKE %s",
                          (PREFIX + '%',))
        dao.conn.commit()
    finally:
        dao.close()


@pytest.fixture
def client():
    _purge()
    yield TestClient(app)
    _purge()


def test_match_without_odds_returns_200_with_nodata(client):
    mid = PREFIX + 'basic'
    _insert_match(mid, (datetime.now(BEIJING) + timedelta(hours=5)).replace(tzinfo=None))

    r = client.get(f'/api/matches/{mid}')

    assert r.status_code == 200, f'无赔率不应返回 {r.status_code}'
    d = r.json()
    assert d['status'] == 'nodata'
    assert d['markets'] == []
    assert d['n_selections'] == 0
    assert d['cost'] is None
    # 比赛基本信息仍要返回，页面要标题和开赛时间
    assert d['home_team'] == 'Home'
    assert d['away_team'] == 'Away'
    assert d['kickoff_time']


def test_unknown_match_still_404(client):
    """真正的「找不到比赛」仍是 404，不能也变成 200。"""
    r = client.get(f'/api/matches/{PREFIX}does_not_exist')
    assert r.status_code == 404
