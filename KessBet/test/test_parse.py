"""新模块 `collect.kessbet.parse` 的测试。

与 `test_kessbet_arb.py` 并存：前者测新模块，后者测尚未重构的旧模块。
两者必须对同样的输入给出同样的输出（阶段 2 的验收标准）。

运行: py -m pytest test/test_parse.py -v
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from collect.kessbet.parse import parse_mari, parse_market_id, split_team


# ---------- parse_mari ----------

def test_parse_simple_1x2():
    mari = '1,245131079416058512/1|245131079416058509,1|245131079416058515,3.15,1,1' \
           '^245131079416058516,3.55,1,X^245131079416058514,2.31,1,2|1x2'
    nm, sels = parse_mari(mari)
    assert nm == '1x2'
    assert sels == [('', '1', 3.15), ('', 'X', 3.55), ('', '2', 2.31)]


def test_parse_fills_line_forward():
    """盘线只在每条盘线的第一个选项上出现，后续要继承。"""
    mari = ('4|245131096595927104,1|0.5,245131100890894585/1=245131100890894756,1.05,1,over'
            '@245131100890894754,11.00,1,under'
            '^2.5,245131100890894633/1=245131100890894716,1.85,1,over'
            '@245131100890894714,2.01,1,under|Total')
    nm, sels = parse_mari(mari)
    assert nm == 'Total'
    assert sels == [
        ('0.5', 'over', 1.05), ('0.5', 'under', 11.00),
        ('2.5', 'over', 1.85), ('2.5', 'under', 2.01),
    ]


def test_parse_handicap_line():
    """让球盘的盘线是 '0:1' 形式。"""
    mari = ('35|245131096595927104,1|0:1,245131100890894647/1=245131100890894908,2.07,1,1'
            '@245131100890894902,3.30,1,X|Handicap')
    nm, sels = parse_mari(mari)
    assert nm == 'Handicap'
    assert sels == [('0:1', '1', 2.07), ('0:1', 'X', 3.30)]


def test_parse_missing_pipe():
    assert parse_mari('') == (None, [])
    assert parse_mari('no pipe here') == (None, [])


def test_parse_skips_bad_odds():
    """赔率不是数字的段应跳过。

    注意副作用：被跳过的段不会更新「当前盘线」，所以后续选项继承到的是
    上一个有效段之前的盘线（这里是空）。这是既有行为，重构时必须保持一致。
    """
    mari = '1|m,1|0.5,id/1=id,abc,1,over@id,1.50,1,under|Total'
    nm, sels = parse_mari(mari)
    assert nm == 'Total'
    assert sels == [('', 'under', 1.50)]


def test_parse_bad_odds_does_not_lose_following_line():
    """赔率非法的段之后如果有新盘线，新盘线仍能正确接管。"""
    mari = ('1|m,1|0.5,id/1=id,abc,1,over@id,1.50,1,under'
            '^2.5,id/1=id,1.85,1,over@id,2.01,1,under|Total')
    nm, sels = parse_mari(mari)
    assert nm == 'Total'
    assert sels == [
        ('', 'under', 1.50),
        ('2.5', 'over', 1.85), ('2.5', 'under', 2.01),
    ]


# ---------- 标签含逗号的盘口（Multiscores） ----------

def test_parse_label_with_commas():
    """Multiscores 的标签本身含逗号，不能被字段切分破坏。"""
    mari = ('94,245145463261532318/1|245127802356012101,1|'
            '245145463261532479,5.40,1,1:0, 2:03:0'
            '^245145463261532482,4.00,1,0:1, 0:20:3|Multiscores')
    nm, sels = parse_mari(mari)
    assert nm == 'Multiscores'
    assert sels == [
        ('', '1:0, 2:03:0', 5.40),
        ('', '0:1, 0:20:3', 4.00),
    ]


def test_parse_label_with_multiple_commas():
    """标签含多个逗号时也要完整保留。"""
    mari = ('94|m,1|245145463261532496,19.00,1,3:2, 4:2, 4:35:1|Multiscores')
    nm, sels = parse_mari(mari)
    assert nm == 'Multiscores'
    assert sels == [('', '3:2, 4:2, 4:35:1', 19.00)]


# ---------- split_team ----------

def test_split_team_home():
    assert split_team('Arsenal Total', 'Arsenal', 'Everton') == ('H', 'Total')


def test_split_team_away():
    assert split_team('Everton Multigoals', 'Arsenal', 'Everton') == ('A', 'Multigoals')


def test_split_team_half_prefix_kept():
    """半场前缀必须保留在基础名里。"""
    assert split_team('1st Half - Arsenal Total', 'Arsenal', 'Everton') == ('H', '1st Half - Total')
    assert split_team('2nd Half - Everton Total', 'Arsenal', 'Everton') == ('A', '2nd Half - Total')


def test_split_team_no_team_prefix():
    assert split_team('Total', 'Arsenal', 'Everton') == ('', 'Total')
    assert split_team('1st Half - Total', 'Arsenal', 'Everton') == ('', '1st Half - Total')


def test_split_team_name_mismatch_keeps_whole_name():
    """队名大小写不符时不应错拆——宁可返回原名，让下游当作未建模跳过。"""
    side, base = split_team('Inter Club De Escaldes Total',
                            'Inter Club de Escaldes', 'Other')
    assert side == ''
    assert base == 'Inter Club De Escaldes Total'


# ---------- parse_market_id ----------

def test_parse_market_id():
    assert parse_market_id('13|m,1|1.5,id/1=id,1.46,1,over|Arsenal Total') == '13'


def test_parse_market_id_empty():
    assert parse_market_id('') == ''


# ---------- 零赔率（停售） ----------

def test_parse_drops_zero_odds():
    """赔率 0.00 是平台标记的停售，应丢弃。"""
    mari = ('2,245150003041964967/1|245143504756445355,1|'
            '245150003041964970,0.00,0,1X'
            '^245150003041964974,1.25,1,12|Double Chance')
    nm, sels = parse_mari(mari)
    assert nm == 'Double Chance'
    assert sels == [('', '12', 1.25)], '0.00 的选项应被丢弃'


def test_parse_drops_zero_odds_with_line():
    """带盘线的零赔率也要丢弃。"""
    mari = ('14|m,1|1.5,id/1=id,8.25,1,over'
            '@id,0.00,0,under|Atletico Grau Total')
    nm, sels = parse_mari(mari)
    assert nm == 'Atletico Grau Total'
    assert sels == [('1.5', 'over', 8.25)]


def test_parse_keeps_odds_above_one():
    """正常赔率不受影响。"""
    mari = '1|m,1|id,1.01,1,1|test'
    nm, sels = parse_mari(mari)
    assert sels == [('', '1', 1.01)]
