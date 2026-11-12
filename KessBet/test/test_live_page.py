"""滚球页面的 API 测试（详情 + 走势）。

不测 HTML 本身 —— 那是纯前端，改样式不该让测试变红。
这里只保证页面依赖的数据接口不会 500、字段齐全。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest

from fastapi.testclient import TestClient

from db.dao import Dao, beijing_now
from web.api import app

MID = '_test_live_page_'
PLATFORM = 'kessbet'


@pytest.fixture(scope='module')
def client():
    return TestClient(app)


@pytest.fixture()
def seeded():
    d = Dao()
    _purge(d)
    try:
        with d.conn.cursor() as c:
            c.execute("""INSERT INTO kessbet_match
                         (platform, match_id, sport, league, country,
                          home_team, away_team, kickoff_time,
                          first_seen_time, updated_time)
                         VALUES (%s,%s,'sr:sport:1','L','C','H','A',
                                 NOW() - INTERVAL 60 MINUTE, NOW(), NOW())""",
                      (PLATFORM, MID))
            c.execute("""INSERT INTO kessbet_match_live
                         (platform, match_id, status, minute, home_score,
                          away_score, home_corner, away_corner,
                          home_yellow, away_yellow, home_red, away_red,
                          kickoff_time, fetched_time)
                         VALUES (%s,%s,31,55,1,0,5,2,1,0,0,0,
                                 NOW() - INTERVAL 60 MINUTE, NOW())""",
                      (PLATFORM, MID))
            # 赔率 5 列齐全，外加 prev_odds —— 详情接口曾经的 bug 就在
            # 这里：SELECT 带了 prev_odds，group_markets 解包报 ValueError
            for mkt, side, line, sel, odds in [
                ('1x2', '', '', '1', 1.85),
                ('1x2', '', '', 'X', 3.50),
                ('1x2', '', '', '2', 3.85),
                ('Total', '', '2.5', 'over', 1.80),
                ('Total', '', '2.5', 'under', 2.00),
            ]:
                c.execute("""INSERT INTO kessbet_odds_live
                             (platform, match_id, market, side, line, selection,
                              odds, prev_odds, kickoff_time, updated_time)
                             VALUES (%s,%s,%s,%s,%s,%s,%s,1.90,
                                     NOW() - INTERVAL 60 MINUTE, NOW())""",
                          (PLATFORM, MID, mkt, side, line, sel, odds))
        d.conn.commit()


        # 走势数据：三笔变动，其中一笔带比分。
        # 必须在 yield 之前写完 —— 放在 yield 之后是 fixture 的
        # teardown 阶段，测试早就跑完了，这个测试会永远看到 0 个点。
        with d.conn.cursor() as c:
            for odds, prev, minute, hs, as_ in [
                (1.90, None, 50, 0, 0),
                (1.85, 1.90, 55, 1, 0),
                (1.80, 1.85, 58, 1, 0),
            ]:
                c.execute("""INSERT INTO kessbet_odds_live_history
                             (platform, match_id, market, side, line, selection,
                              odds, prev_odds, minute, home_score, away_score,
                              kickoff_time, changed_time)
                             VALUES (%s,%s,'1x2','','','1',%s,%s,%s,%s,%s,
                                     NOW() - INTERVAL 60 MINUTE, NOW())""",
                          (PLATFORM, MID, odds, prev, minute, hs, as_))
        d.conn.commit()

        yield d
    finally:
        pass


def _purge(d):
    with d.conn.cursor() as c:
        for t in ('kessbet_odds_live', 'kessbet_odds_live_history',
                  'kessbet_match_live',
                  'kessbet_match'):
            c.execute(f"DELETE FROM {t} WHERE platform=%s AND match_id=%s",
                      (PLATFORM, MID))
    d.conn.commit()


@pytest.fixture()
def clean(seeded):
    d = seeded
    yield
    _purge(d)
    d.close()


# ---------------------------------------------------------------- 详情

def test_live_detail_returns_markets(client, clean):
    """详情必须返回盘口 —— 之前 SELECT 多带 prev_odds 会 500。"""
    r = client.get(f'/api/live/{MID}')
    assert r.status_code == 200
    d = r.json()
    assert len(d['markets']) == 2          # 1x2 + Total
    assert d['n_markets'] == 2
    assert d['n_selections'] == 5


def test_live_detail_status_label(client, clean):
    """ms=31 是中场休息，不能显示成「未知(31)」。"""
    d = client.get(f'/api/live/{MID}').json()
    assert d['status_label'] == '中场休息'


@pytest.mark.parametrize('code,expect', [
    (0, '未开始'),
    (6, '进行中'),
    (7, '进行中'),
    (31, '中场休息'),
    (100, '已结束'),
    (120, '点球大战'),
])
def test_status_label_known_codes(code, expect):
    from web.api import _status_label
    assert _status_label(code) == expect


def test_status_label_unknown_code_not_scary():
    """未知码显示成「进行中 · 码 X」，不是「未知(X)」。

    状态码是平台私有编号，实测美式足球出现过 303。显示「未知」会让
    用户以为数据坏了，实际上那场比赛就是在踢。
    """
    from web.api import _status_label
    label = _status_label(303)
    assert '未知' not in label
    assert '303' in label          # 保留原值方便排查


def test_status_label_none():
    from web.api import _status_label
    assert _status_label(None) == '—'


def test_half_time_counts_as_in_play(client, clean):
    """中场休息必须出现在「只看进行中」里。

    这里曾经写死 `status IN (6,7)`，把 ms=31 漏掉了 —— 中场当然
    还算进行中。改成黑名单 `NOT IN (0,100,120)` 才对。
    """
    r = client.get('/api/live', params={'only_in_play': 'true'})
    assert r.status_code == 200
    ids = [m['match_id'] for m in r.json()['matches']]
    assert MID in ids, '中场休息的比赛被「只看进行中」过滤掉了'


def test_live_detail_404(client):
    r = client.get('/api/live/_no_such_match_')
    assert r.status_code == 404


# ---------------------------------------------------------------- 走势

def test_live_history_points(client, clean):
    r = client.get(f'/api/live/{MID}/history',
                   params={'market': '1x2', 'label': '1'})
    assert r.status_code == 200
    pts = r.json()['points']
    assert len(pts) == 3
    assert [p['odds'] for p in pts] == [1.90, 1.85, 1.80]
    # 每点带比分上下文 —— 这是滚球走势和赛前走势的区别
    assert pts[1]['minute'] == 55
    assert pts[1]['home_score'] == 1


def test_live_history_empty_for_unknown_selection(client, clean):
    r = client.get(f'/api/live/{MID}/history',
                   params={'market': '1x2', 'label': '2'})
    assert r.status_code == 200
    assert r.json()['points'] == []


# ---------------------------------------------------------------- 页面路由

@pytest.mark.parametrize('path', [
    '/live',
    f'/live/{MID}',
    f'/live/detail?id={MID}',
])
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert b'<html' in r.content[:200].lower()
