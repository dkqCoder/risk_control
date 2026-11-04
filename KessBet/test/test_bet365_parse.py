"""bet365 pod 解析的回归测试。

fixture 是 2026-11-03 从 `homepagecontentapi/pod?pd=#HO#P31524#` 抓的真实响应
（261 KB，9 场比赛 / 33 个选项 / 5 种盘口）。保留它是为了让解析逻辑
一直对着真实格式验证，而不是对着我编的例子。
"""
import os

import pytest

from collect.bet365 import parse

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'bet365_home_pod.txt')


@pytest.fixture(scope='module')
def pod_text():
    with open(FIXTURE, encoding='utf-8') as f:
        return f.read()


# ---------- 分数赔率换算 ----------

@pytest.mark.parametrize('raw,expected', [
    ('6/5', 2.2),
    ('1/1', 2.0),
    ('1/3', 1.3333),
    ('27/100', 1.27),
    ('4/6', 1.6667),
])
def test_fractional_to_decimal(raw, expected):
    assert parse.fractional_to_decimal(raw) == pytest.approx(expected, abs=1e-4)


@pytest.mark.parametrize('raw', ['', '   ', 'abc', '1/0', '/', 'x/y'])
def test_fractional_to_decimal_rejects_garbage(raw):
    assert parse.fractional_to_decimal(raw) is None


def test_decimal_passthrough():
    """已经是小数形式的直接返回，不再 +1。"""
    assert parse.fractional_to_decimal('2.20') == pytest.approx(2.2)


# ---------- 记录流切分 ----------

def test_parse_records_splits_types(pod_text):
    recs = parse.parse_records(pod_text)
    assert len(recs) > 1000
    types = {t for t, _ in recs}
    assert {'MG', 'MA', 'CL'} <= types
    # 控制符必须被剥掉
    assert not any(t.startswith('\x08') for t, _ in recs)


def test_parse_records_keeps_values_with_equals():
    recs = parse.parse_records('MA;NA=a=b;OD=1/1;|')
    assert recs == [('MA', {'NA': 'a=b', 'OD': '1/1'})]


# ---------- 比赛与赔率 ----------

def test_parse_events_real_snapshot(pod_text):
    events = parse.parse_events(pod_text)
    assert len(events) == 9
    names = {e['name'] for e in events}
    assert '加拉塔萨雷 v 斯图加特' in names
    gal = next(e for e in events if e['name'] == '加拉塔萨雷 v 斯图加特')
    assert gal['home'] == '加拉塔萨雷'
    assert gal['away'] == '斯图加特'
    assert gal['competition'] == 'UEFA-CL'


def test_parse_events_dedupes(pod_text):
    """同一场比赛在首页会重复出现，必须去重。"""
    events = parse.parse_events(pod_text)
    names = [e['name'] for e in events]
    assert len(names) == len(set(names))


def test_parse_odds_real_snapshot(pod_text):
    odds = parse.parse_odds(pod_text)
    assert len(odds) == 9
    total = sum(len(v) for v in odds.values())
    assert total == 33

    markets = {r['market'] for rows in odds.values() for r in rows}
    assert markets == {'全场赛果', '进球总数', '两支球队都得分',
                       '赛果/两队均得分', '在任何时间进球'}


def test_parse_odds_1x2_row(pod_text):
    """1X2 行：选择项是队名，EX 是对手名，赔率是分数。"""
    rows = parse.parse_odds(pod_text)['加拉塔萨雷 v 斯图加特']
    x12 = next(r for r in rows if r['market'] == '全场赛果')
    assert x12['selection'] == '加拉塔萨雷'
    assert x12['opponent'] == '斯图加特'
    assert x12['odds_raw'] == '1/1'
    assert x12['odds'] == pytest.approx(2.0)


def test_parse_odds_totals_row(pod_text):
    """大小球行：盘线在 HO 字段。"""
    rows = parse.parse_odds(pod_text)['加拉塔萨雷 v 斯图加特']
    tot = next(r for r in rows if r['market'] == '进球总数')
    assert tot['selection'] == '超过'
    assert tot['line'] == '2.5'


def test_parse_odds_requires_n2_mn_od():
    """缺任一关键字段的行必须丢弃（首页混着串关、广告等非赔率行）。"""
    text = (
        'MA;NA=有比赛;N2=主 v 客;MN=全场赛果;OD=1/1;|'
        'MA;NA=缺比赛;MN=全场赛果;OD=1/1;|'
        'MA;NA=缺盘口;N2=主 v 客;OD=1/1;|'
        'MA;NA=缺赔率;N2=主 v 客;MN=全场赛果;OD=;|'
        'MA;NA=坏赔率;N2=主 v 客;MN=全场赛果;OD=abc;|'
    )
    odds = parse.parse_odds(text)
    assert list(odds) == ['主 v 客']
    assert len(odds['主 v 客']) == 1


def test_summarize(pod_text):
    s = parse.summarize(pod_text)
    assert s['events'] == 9
    assert s['selections'] == 33
    assert s['markets'] == 5
    assert s['bytes'] == len(pod_text)
