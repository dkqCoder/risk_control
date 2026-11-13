"""bet365 联赛解析的回归测试。

fixture 是 2026-11-09 从英超（E91422157）的 1X2 盘口抓的真实响应，
21 KB / 20 场比赛 / 60 个赔率。保留真实数据是为了让解析一直对得上线上格式。
"""
import os

import pytest

from collect.bet365 import league

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'bet365_league_1x2.txt')


@pytest.fixture(scope='module')
def text():
    with open(FIXTURE, encoding='utf-8') as f:
        return f.read()


# ---------- 比赛表 ----------

def test_parse_fixtures_count(text):
    fixtures = league.parse_fixtures(text)
    assert len(fixtures) == 20


def test_parse_fixtures_fields(text):
    fx = league.parse_fixtures(text)
    first = fx[0]
    assert first['home'] == '曼城'
    assert first['away'] == '富勒姆'
    assert first['name'] == '曼城 v 富勒姆'
    assert first['fixture_id'] == '203186235'
    assert first['pz'] == 0


def test_parse_fixtures_sportradar_id(text):
    """EX 字段里的 Sportradar 链接要抽出来，这是跨平台对齐的关键。"""
    fx = league.parse_fixtures(text)
    first = fx[0]
    assert first['sportradar_id'] == 'sr:match:72221406'


def test_parse_fixtures_kickoff(text):
    """BC 是英国时间 yyyyMMddHHmmss。"""
    fx = league.parse_fixtures(text)
    first = fx[0]
    assert first['kickoff_uk'].year == 2026
    assert first['kickoff_uk'].month == 11
    assert first['kickoff_uk'].hour == 12


def test_parse_fixtures_dedupes(text):
    """同一场比赛在多个盘口分组里会重复出现，必须按 FI 去重。"""
    fx = league.parse_fixtures(text)
    ids = [f['fixture_id'] for f in fx]
    assert len(ids) == len(set(ids))


def test_parse_fixtures_requires_v_separator():
    """比赛名必须含 ' v ' 才认，否则跳过。"""
    t = ('PA;ID=PC1;FI=1;FD=没有分隔符;PZ=0;|'
         'PA;ID=PC2;FI=2;FD=Home v Away;PZ=1;|')
    assert len(league.parse_fixtures(t)) == 1


# ---------- 盘口目录 ----------

def test_parse_market_catalog(text):
    markets = league.parse_market_catalog(text)
    names = [m['market'] for m in markets]
    assert '全场赛果' in names
    assert '进球总数' in names
    # 每个都要带可用的 PD 路径
    assert all(m['pd'].startswith('#AC#') for m in markets)


# ---------- 赔率 ----------

def test_parse_market_odds_count(text):
    rows = league.parse_market_odds(text)
    assert len(rows) == 60  # 20 场 × 1/X/2


def test_parse_market_odds_labels(text):
    """MA 行是列头；其后连续的 PA 行按 PZ 对齐到各场比赛。"""
    rows = league.parse_market_odds(text)
    labels = {r['selection'] for r in rows}
    assert labels == {'1', 'X', '2'}


def test_parse_market_odds_not_fixture_rows(text):
    """比赛表本身的行（ID=PC...）不能被当成赔率。"""
    rows = league.parse_market_odds(text)
    # 比赛表 20 行没有 OD，本来就会被过滤；再确认数量对得上
    assert len(rows) == 60


def test_build_market_table_real_values(text):
    """对着真实数据校验具体赔率值，防止解析错位。"""
    table = league.build_market_table(text)
    assert len(table) == 20

    city = table['曼城 v 富勒姆']
    assert city['1'] == pytest.approx(1.48)
    assert city['X'] == pytest.approx(4.5)
    assert city['2'] == pytest.approx(6.0)

    newcastle = table['纽卡斯尔 v 阿森纳']
    assert newcastle['1'] == pytest.approx(5.0)
    assert newcastle['2'] == pytest.approx(1.6)


def test_overround_is_realistic(text):
    """覆盖成本应落在 1.0~1.15，明显偏离说明解析错位。"""
    table = league.build_market_table(text)
    costs = []
    for sels in table.values():
        if all(k in sels for k in ('1', 'X', '2')):
            costs.append(sum(1 / v for v in sels.values()))
    assert len(costs) == 20
    assert all(1.0 < c < 1.15 for c in costs), f'异常覆盖成本: {costs}'
    assert 1.03 < sum(costs) / len(costs) < 1.12


def test_summarize(text):
    s = league.summarize(text)
    assert s['fixtures'] == 20
    assert s['odds_rows'] == 60
    assert s['bytes'] == len(text)
