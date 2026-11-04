"""KessBet 解析与套利模块的回归测试。

覆盖 HANDOFF 里记录过的坑（盘线向下填充、other 补集、分支顺序）
以及修复的缺陷（队名盘口、半场比分、斜杠写法的两串盘）。

运行: py -m pytest test_kessbet_arb.py -v
"""
import json
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import kessbet_arb as P

SNAP = os.path.join(BASE, 'data', 'snapshots', '_api_snapshot.json')
HOME, AWAY = 'Arsenal', 'Everton'


def load_markets():
    """按盘口名索引 _api_snapshot.json 里的原始 mari。"""
    raw = json.load(open(SNAP, encoding='utf-8'))['data']
    out = {}
    for mk in raw:
        nm, sels = P.parse_mari(mk['mari'])
        if nm:
            out[nm] = (mk['mari'], sels)
    return out


@pytest.fixture
def snap_file(tmp_path):
    """collect() 接受的是盘口数组文件，而 _api_snapshot.json 包了一层 {ts, data}。"""
    raw = json.load(open(SNAP, encoding='utf-8'))['data']
    p = tmp_path / 'markets.json'
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
    return str(p)


def count(mask):
    return bin(mask).count('1') if mask is not None else None


# ---------- split_team ----------

def test_split_team_strips_half_prefix_and_team():
    """'1st Half - Arsenal Total' 应拆成 ('H', '1st Half - Total')。"""
    assert P.split_team('1st Half - Arsenal Total', HOME, AWAY) == ('H', '1st Half - Total')


def test_split_team_plain_team_market():
    assert P.split_team('Arsenal Total', HOME, AWAY) == ('H', 'Total')
    assert P.split_team('Everton Multigoals', HOME, AWAY) == ('A', 'Multigoals')


def test_split_team_generic_market_untouched():
    assert P.split_team('Total', HOME, AWAY) == ('', 'Total')
    assert P.split_team('1st Half - Total', HOME, AWAY) == ('', '1st Half - Total')


# ---------- collect: 队名盘口必须进入候选池 ----------

def test_collect_includes_team_specific_markets(snap_file):
    """修复前 collect 传 nm 给 covers，队名盘口全部丢失。"""
    cands = P.collect(snap_file, HOME, AWAY)
    labels = {d for _, _, d in cands}
    assert 'Arsenal Total|1.5|over' in labels
    assert '1st Half - Arsenal Total|0.5|over' in labels


def test_collect_includes_half_correct_score(snap_file):
    """半场比分盘既贡献 other 补集，也贡献具名比分本身。

    具名比分曾被认为「1/赔率 太大，不会进最优解」而整类丢弃，
    实测该理由不成立 —— 详见
    docs/superpowers/specs/2026-11-04-correct-score-named-review.md
    """
    cands = P.collect(snap_file, HOME, AWAY)
    labels = {d for _, _, d in cands}
    assert '1st Half - Correct Score|other' in labels
    named = {d for d in labels if d.startswith('1st Half - Correct Score||')}
    assert named, '具名半场比分应进入候选池'


def test_collect_half_correct_score_other_is_complement(snap_file):
    """other 应覆盖未被列出的半场比分，并只看半场维度。"""
    cands = P.collect(snap_file, HOME, AWAY)
    masks = [m for m, _, d in cands if d == '1st Half - Correct Score|other']
    assert len(masks) == 1

    listed = [(0, 0), (1, 1), (2, 2), (1, 0), (2, 0), (2, 1), (0, 1), (0, 2), (1, 2)]
    expected = P.FULL
    for hh, aa in listed:
        expected &= ~P.build(lambda h1, a1, h2, a2, x=hh, y=aa: h1 == x and a1 == y)
    assert masks[0] == expected

    # 半场 0:0 已被具名列出 -> other 不覆盖
    assert not (masks[0] >> P.IDX[(0, 0, 0, 0)]) & 1
    # 半场 3:0 未列出 -> other 覆盖（无论全场如何）
    assert (masks[0] >> P.IDX[(3, 0, 3, 0)]) & 1
    assert (masks[0] >> P.IDX[(3, 0, 5, 5)]) & 1


# ---------- covers: 半场比分语义 ----------

def test_half_correct_score_other_maps_half_time_not_full_time():
    """半场比分盘的 '1:0' 语义必须是半场 1:0，与全场无关。"""
    m = P.build(lambda h1, a1, h2, a2: h1 == 1 and a1 == 0)
    # 半场 1:0 且全场 5:5 —— 覆盖
    assert (m >> P.IDX[(1, 0, 5, 5)]) & 1
    # 半场 0:0 且全场 1:0 —— 不覆盖
    assert not (m >> P.IDX[(0, 0, 1, 0)]) & 1


# ---------- covers: Double Chance & Total 的斜杠写法 ----------

def test_double_chance_and_total_slash_labels_modeled():
    """实际标签是 '1/X & over'；修复前只有 1X/12/X2 写法能匹配。"""
    for lab in ['1/X & over', '1/2 & over', 'X/2 & over', '1/X & under']:
        m = P.covers('Double Chance & Total', '', '1.5', lab)
        assert m is not None, f'{lab} 未被建模'


def test_double_chance_and_total_slash_equals_bare_form():
    """'1/X & over' 与 '1X & over' 必须覆盖完全相同的结果集。"""
    a = P.covers('Double Chance & Total', '', '1.5', '1/X & over')
    b = P.covers('Double Chance & Total', '', '1.5', '1X & over')
    assert a == b


# ---------- covers: 既有语义不能被破坏 ----------

def test_correct_score_covers_is_none_by_design():
    """Correct Score 由 collect 特殊处理（需拼 other 补集），covers 必须返回 None。"""
    assert P.covers('Correct Score', '', '', '2:1') is None


def test_team_total_uses_side():
    """side='H' 的主队总进球 1.5 over：全场主队 >=2。"""
    m = P.covers('Total', 'H', '1.5', 'over')
    assert m is not None
    assert (m >> P.IDX[(0, 0, 2, 0)]) & 1
    assert not (m >> P.IDX[(0, 0, 1, 0)]) & 1
    assert not (m >> P.IDX[(0, 0, 1, 5)]) & 1


# ---------- Clean Sheet ----------

def test_clean_sheet_yes_is_modeled():
    """主队零封：客队全场 0 球。曾被注释吞掉整个 elif 分支。"""
    m = P.covers('Clean Sheet', 'H', '', 'yes')
    assert m is not None, 'Clean Sheet 分支是死代码'
    assert (m >> P.IDX[(0, 0, 3, 0)]) & 1        # 3:0 主队零封
    assert not (m >> P.IDX[(0, 0, 3, 1)]) & 1    # 3:1 客队有球


def test_clean_sheet_away_side():
    """客队零封：主队全场 0 球。"""
    m = P.covers('Clean Sheet', 'A', '', 'yes')
    assert m is not None
    assert (m >> P.IDX[(0, 0, 0, 2)]) & 1
    assert not (m >> P.IDX[(0, 0, 1, 2)]) & 1


def test_clean_sheet_no_is_complement():
    """'no' 与 'yes' 应互为补集。"""
    yes = P.covers('Clean Sheet', 'H', '', 'yes')
    no = P.covers('Clean Sheet', 'H', '', 'no')
    assert yes is not None and no is not None
    assert yes | no == P.FULL
    assert yes & no == 0


# ---------- 2nd Half ----------

def test_second_half_total_uses_second_half_goals():
    """下半场 1.5 大：下半场进球 (h2-h1)+(a2-a1) > 1.5，与上半场无关。"""
    m = P.covers('2nd Half - Total', '', '1.5', 'over')
    assert m is not None, '2nd Half - Total 未建模'
    assert (m >> P.IDX[(0, 0, 1, 1)]) & 1        # 下半场 2 球
    assert not (m >> P.IDX[(2, 2, 2, 2)]) & 1    # 上半场进 4 球，下半场 0 球
    assert not (m >> P.IDX[(0, 0, 1, 0)]) & 1    # 下半场 1 球


def test_second_half_1x2_uses_second_half_result():
    """下半场 1x2 看的是下半场比分，不是全场。"""
    m = P.covers('2nd Half - 1x2', '', '', '1')
    assert m is not None, '2nd Half - 1x2 未建模'
    assert (m >> P.IDX[(0, 2, 1, 2)]) & 1        # 全场 1:2，下半场 1:0
    assert not (m >> P.IDX[(2, 0, 2, 0)]) & 1    # 全场 2:0，下半场 0:0


def test_second_half_total_over_under_partition():
    """下半场某盘线的 over/under 必须划分状态空间。"""
    o = P.covers('2nd Half - Total', '', '1.5', 'over')
    u = P.covers('2nd Half - Total', '', '1.5', 'under')
    assert o is not None and u is not None
    assert o | u == P.FULL
    assert o & u == 0


def test_second_half_odd_even_partition():
    """下半场单双：两选项划分状态空间。"""
    odd = P.covers('2nd Half - Odd/Even', '', '', 'odd')
    even = P.covers('2nd Half - Odd/Even', '', '', 'even')
    assert odd is not None and even is not None
    assert odd | even == P.FULL
    # 下半场进 1 球为奇
    assert (odd >> P.IDX[(0, 0, 1, 0)]) & 1
    assert (even >> P.IDX[(0, 0, 1, 1)]) & 1


def test_1x2_covers_are_partition():
    """1X2 三项必须恰好划分状态空间。"""
    a = P.covers('1x2', '', '', '1')
    b = P.covers('1x2', '', '', 'X')
    c = P.covers('1x2', '', '', '2')
    assert a | b | c == P.FULL
    assert (a & b) == 0 and (a & c) == 0 and (b & c) == 0
