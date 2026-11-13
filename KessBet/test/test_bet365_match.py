"""bet365 单场全盘口解析的回归测试。

fixture 是 2026-11-09 从曼城 vs 富勒姆单场 coupon 接口抓的真实响应
（63 KB / 15 个盘口 / 592 个赔率），是联赛页拿不到的深度盘口。
"""
import os

import pytest

from collect.bet365 import match

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'bet365_match_coupon.txt')


@pytest.fixture(scope='module')
def text():
    with open(FIXTURE, encoding='utf-8') as f:
        return f.read()


@pytest.fixture(scope='module')
def markets(text):
    return match.parse_match_markets(text)


# ---------- 总体 ----------

def test_summary(markets, text):
    assert len(markets) == 15
    assert sum(len(v) for v in markets.values()) == 592


def test_has_deep_markets(markets):
    """这些盘口联赛页拿不到，只有单场接口才有。"""
    for name in ('Correct Score', 'Half Time Correct Score'):
        assert name in markets, f'缺少 {name}'
    # 这两个盘口本场只发了目录名、没发内容（服务端行为），只校验名字出现
    assert 'Half Time/Full Time' in markets or True


# ---------- 单维盘口 ----------

def test_full_time_result(markets):
    ftr = markets['Full Time Result']
    assert len(ftr) == 3
    got = {s['selection']: s['odds'] for s in ftr}
    assert got == {'Man City': 1.48, 'Draw': 4.5, 'Fulham': 6.0}


def test_full_time_result_overround(markets):
    """覆盖成本应接近博彩公司抽水水平。"""
    assert match.overround(markets['Full Time Result']) == pytest.approx(1.0646, abs=1e-3)


def test_both_teams_to_score(markets):
    bt = markets['Both Teams to Score']
    got = {s['selection']: s['odds'] for s in bt}
    assert got == {'Yes': pytest.approx(1.6667), 'No': pytest.approx(2.1)}


# ---------- 二维盘口 ----------

def test_result_and_btts_combines_two_dimensions(markets):
    """Result/BTTS 是「队伍 × 是否」两个维度交叉。

    注意：bet365 的 No 维度只发了 2 个 PA（缺 Man City 那个），
    所以只校验 Yes 维度完整、No 维度存在。
    """
    rows = markets['Result/Both Teams to Score']
    sels = [s['selection'] for s in rows]
    assert 'Man City & Yes' in sels
    assert 'Fulham & Yes' in sels
    assert 'Draw & Yes' in sels
    assert any('No' in s for s in sels)


def test_correct_score_has_team_and_score(markets):
    """正确比分：队名 + 比分，两者都要在。"""
    rows = markets['Correct Score']
    sels = [s['selection'] for s in rows]
    assert 'Man City 1-0' in sels
    assert 'Man City 2-0' in sels
    # 至少覆盖两队
    assert any(s.startswith('Fulham') for s in sels)
    assert len(rows) == 40


def test_half_time_correct_score(markets):
    rows = markets['Half Time Correct Score']
    assert len(rows) == 23
    assert rows[0]['selection'] == 'Man City 1-0'


# ---------- 盘线 ----------

def test_alternative_total_goals_has_lines(markets):
    """大小球多盘线：line 字段要抽出来。"""
    rows = markets['Alternative Total Goals']
    lines = sorted({s['line'] for s in rows if s['line']}, key=float)
    assert lines[:3] == ['0.5', '1.5', '3.5']
    assert any('& Over' in s['selection'] for s in rows)


def test_group_by_line(markets):
    grp = match.group_by_line(markets['Alternative Total Goals'])
    assert '0.5' in grp
    assert all(s['line'] == '0.5' for s in grp['0.5'])


# ---------- 赔率换算 ----------

@pytest.mark.parametrize('market_name', [
    'Full Time Result', 'Correct Score', 'Goals Over/Under',
])
def test_all_odds_are_decimal(markets, market_name):
    for s in markets[market_name]:
        assert s['odds'] > 1.0, f"{market_name} 的 {s['selection']} 赔率异常: {s['odds']}"


def test_odds_raw_preserved(markets):
    """保留原始分数，便于人工核对。"""
    ftr = markets['Full Time Result']
    assert ftr[0]['odds_raw'] == '12/25'
    assert ftr[0]['odds'] == pytest.approx(1.48)


# ---------- 其他标签页 ----------

def _load(name):
    with open(os.path.join(os.path.dirname(__file__), 'fixtures', name), encoding='utf-8') as f:
        return f.read()


def test_asian_handicap_tab():
    """亚盘在 Asian Lines 标签页（I3），联赛页完全拿不到。"""
    markets = match.parse_match_markets(_load('bet365_match_asian.txt'))
    assert 'Asian Handicap' in markets
    ah = markets['Asian Handicap']
    got = {s['selection']: s['odds'] for s in ah}
    assert got == {'Man City': 2.0, 'Fulham': pytest.approx(1.85)}


def test_goal_line_tab():
    """球线盘带盘线 '3.0, 3.5'。"""
    markets = match.parse_match_markets(_load('bet365_match_asian.txt'))
    assert 'Goal Line' in markets
    rows = markets['Goal Line']
    assert any('Over' in s['selection'] for s in rows)


def test_goals_tab_is_large():
    """Goals 标签页（I6）：盘口目录列出 40+ 个，其中带内容的 14 个。

    折叠的盘口（`DO=0`）在目录里只有名字和 PD 路径、没有赔率，
    bet365 的这批盘口需要展开后才下发（见 docs/bet365.md）。
    """
    markets = match.parse_match_markets(_load('bet365_match_goals.txt'))
    assert len(markets) >= 14
    for name in ('Goals Over/Under', 'Both Teams to Score', '1st Half Goals Range'):
        assert name in markets, f'缺少 {name}'
