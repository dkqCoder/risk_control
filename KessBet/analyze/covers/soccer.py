"""analyze/covers/soccer.py — 足球盘口语义。

状态空间 (h1, a1, h2, a2)：半场主客进球 + 全场主客进球。
约束 h2>=h1 且 a2>=a1（全场进球不可能少于半场）。

把每个盘口选项映射成一个覆盖状态集合的 bitmask，供 solver 求最小覆盖成本。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from collect.kessbet.parse import parse_mari, split_team


H = 6      # 半场 0..5
G = 10     # 全场 0..9

STATES = [(h1, a1, h2, a2)
          for h1 in range(H) for a1 in range(H)
          for h2 in range(h1, G) for a2 in range(a1, G)]
NS = len(STATES)
IDX = {s: i for i, s in enumerate(STATES)}
FULL = (1 << NS) - 1

_cache = {}


def build(pred):
    m = 0
    for i, s in enumerate(STATES):
        if pred(*s):
            m |= 1 << i
    return m


def _h(line):
    try:
        a, b = line.split(':')
        return float(a), float(b)
    except Exception:
        return None


# ---------- 半场拆分辅助 ----------
# 状态是 (h1, a1, h2, a2)：
#   上半场 = (h1, a1)
#   下半场 = (h2-h1, a2-a1)

def _half_goals(state, side, first):
    """某队在某半场的进球数。side='H'/'A'。"""
    h1, a1, h2, a2 = state
    if first:
        return h1 if side == 'H' else a1
    return (h2 - h1) if side == 'H' else (a2 - a1)


def _half_total(state, first):
    """某半场的双方总进球。"""
    h1, a1, h2, a2 = state
    if first:
        return h1 + a1
    return (h2 - h1) + (a2 - a1)


def _half_win(state, side, first):
    """某队在某半场是否领先。"""
    h1, a1, h2, a2 = state
    if first:
        return h1 > a1 if side == 'H' else a1 > h1
    sh, sa = h2 - h1, a2 - a1
    return sh > sa if side == 'H' else sa > sh


def _result_of(hg, ag):
    return '1' if hg > ag else ('X' if hg == ag else '2')


def _or_condition(market, side, line):
    """'Or <队伍> ...' 类盘口的 Yes 条件。

    官方规则（接口 d 字段）：
      Or Over 2.5   : "home team will win OR total goals is over x.5"
      Or Under 2.5  : "home team will win OR total goals is under x.5"
      Or Both Teams To Score : "home team will win OR both teams score"
      Or Any Clean Sheet     : "home team will win OR any team clean sheet"
    side='H' 表示主队，'A' 表示客队。
    """
    team_wins = lambda state: _result_of(state[2], state[3]) == ('1' if side == 'H' else '2')
    scored_side_clean = 'no'

    if market == 'Or Over 2.5' and line is not None:
        return build(lambda *s: team_wins(s) or (s[2] + s[3]) > line)
    if market == 'Or Under 2.5' and line is not None:
        return build(lambda *s: team_wins(s) or (s[2] + s[3]) < line)
    if market == 'Or Both Teams To Score':
        return build(lambda *s: team_wins(s) or (s[2] > 0 and s[3] > 0))
    if market == 'Or Any Clean Sheet':
        # "any of the teams will keep a clean sheet"
        return build(lambda *s: team_wins(s) or s[2] == 0 or s[3] == 0)
    return None


def _draw_or_condition(market, line):
    """'Draw Or ...' 类盘口的 Yes 条件。

    官方规则：
      Draw Or Over 2.5  : "match will end with a draw OR total goals over x.5"
      Draw Or Under 2.5 : "match will end with a draw OR total goals under x.5"
      Draw Or Both Teams To Score : "draw OR both teams score"
      Draw Or Any Clean Sheet     : "draw OR any team clean sheet"
    """
    is_draw = lambda s: s[2] == s[3]

    if market == 'Draw Or Over 2.5' and line is not None:
        return build(lambda *s: is_draw(s) or (s[2] + s[3]) > line)
    if market == 'Draw Or Under 2.5' and line is not None:
        return build(lambda *s: is_draw(s) or (s[2] + s[3]) < line)
    if market == 'Draw Or Both Teams To Score':
        return build(lambda *s: is_draw(s) or (s[2] > 0 and s[3] > 0))
    if market == 'Draw Or Any Clean Sheet':
        return build(lambda *s: is_draw(s) or s[2] == 0 or s[3] == 0)
    return None


def covers(market, side, line, label):
    """返回该选项覆盖的状态 bitmask，无法建模返回 None。"""
    k = (market, side, line, label)
    if k in _cache:
        return _cache[k]
    r = None
    # 1st Half = 上半场（用 h1/a1），2nd Half = 下半场（用 h2-h1 / a2-a1）
    is_half = market.startswith('1st Half - ')
    is_2nd = market.startswith('2nd Half - ')
    is_sub = is_half or is_2nd   # 上半场或下半场
    if is_half:
        base = market[len('1st Half - '):]
    elif is_2nd:
        base = market[len('2nd Half - '):]
    else:
        base = market

    def F(h1, a1, h2, a2):   # 全场
        return (h2, a2)

    def HF1(h1, a1, h2, a2):  # 上半场
        return (h1, a1)

    def HF2(h1, a1, h2, a2):  # 下半场
        return (h2 - h1, a2 - a1)

    # 半场分支统一走 HF：1st Half 用上半场，2nd Half 用下半场
    HF = HF2 if is_2nd else HF1

    def total(h1, a1, h2, a2):
        return h2 + a2

    def htotal(h1, a1, h2, a2):
        return ((h2 - h1) + (a2 - a1)) if is_2nd else (h1 + a1)

    def tg(h1, a1, h2, a2):
        return h2 if side == 'H' else a2

    def htg(h1, a1, h2, a2):
        if is_2nd:
            return (h2 - h1) if side == 'H' else (a2 - a1)
        return h1 if side == 'H' else a1

    def win(h1, a1, h2, a2):
        return h2 > a2 if side == 'H' else a2 > h2

    def hwin(h1, a1, h2, a2):
        return h1 > a1 if side == 'H' else a1 > h1

    def res(hg, ag):
        return '1' if hg > ag else ('X' if hg == ag else '2')

    # ---------- 全场 ----------
    if base == '1x2' and not side and not is_sub:
        r = build(lambda *s: res(*F(*s)) == label)
    elif base == 'Double Chance' and not side and not is_sub:
        ok = {'1X': '1X', '12': '12', 'X2': 'X2'}.get(label)
        if ok:
            r = build(lambda *s: res(*F(*s)) in ok)
    elif base == 'Both Teams To Score' and not side and not is_sub:
        if label == 'yes':
            r = build(lambda *s: F(*s)[0] > 0 and F(*s)[1] > 0)
        elif label == 'no':
            r = build(lambda *s: F(*s)[0] == 0 or F(*s)[1] == 0)
    elif base == 'Total' and not side and not is_sub:
        L = float(line) if line else None
        if L is not None:
            if label == 'over':
                r = build(lambda *s: total(*s) > L)
            elif label == 'under':
                r = build(lambda *s: total(*s) < L)
    elif base == 'Total' and not is_sub and side:
        L = float(line) if line else None
        if L is not None:
            if label == 'over':
                r = build(lambda *s: tg(*s) > L)
            elif label == 'under':
                r = build(lambda *s: tg(*s) < L)
    elif base == 'Handicap' and not side and not is_sub:
        hp = _h(line)
        if hp:
            hl, al = hp
            if label == '1':
                r = build(lambda *s: F(*s)[0] + hl > F(*s)[1] + al)
            elif label == 'X':
                r = build(lambda *s: F(*s)[0] + hl == F(*s)[1] + al)
            elif label == '2':
                r = build(lambda *s: F(*s)[0] + hl < F(*s)[1] + al)
    elif base == 'Exact Goals' and not side and not is_sub:
        if label.endswith('+'):
            kk = int(label[:-1])
            r = build(lambda *s: total(*s) >= kk)
        elif label.isdigit():
            kk = int(label)
            r = build(lambda *s: total(*s) == kk)
    elif base == 'Exact Goals' and not is_sub and side:
        if label.endswith('+'):
            kk = int(label[:-1])
            r = build(lambda *s: tg(*s) >= kk)
        elif label.isdigit():
            kk = int(label)
            r = build(lambda *s: tg(*s) == kk)
    elif base == 'Multigoals' and not side and not is_sub:
        if label == 'no goal':
            r = build(lambda *s: total(*s) == 0)
        elif label.endswith('+'):
            kk = int(label[:-1])
            r = build(lambda *s: total(*s) >= kk)
        elif '-' in label:
            lo, hi = map(int, label.split('-'))
            r = build(lambda *s: lo <= total(*s) <= hi)
    elif base == 'Multigoals' and not is_sub and side:
        if label == 'no goal':
            r = build(lambda *s: tg(*s) == 0)
        elif label.endswith('+'):
            kk = int(label[:-1])
            r = build(lambda *s: tg(*s) >= kk)
        elif '-' in label:
            lo, hi = map(int, label.split('-'))
            r = build(lambda *s: lo <= tg(*s) <= hi)
    elif base == 'Goal Range' and not side and not is_sub:
        # 平台有两种写法，都要支持：
        #   主流（314 场）：0-1 / 2-3 / 4-5 / 6+
        #   少数（15 场）： 0-1 / 2-3 / 4-6 / 7+
        rng = {
            '0-1': (0, 1), '2-3': (2, 3),
            '4-5': (4, 5), '4-6': (4, 6),
            '6+': (6, 99), '7+': (7, 99),
        }
        if label in rng:
            lo, hi = rng[label]
            r = build(lambda *s: lo <= total(*s) <= hi)
    elif base == 'Winning Margin' and not side and not is_sub:
        mm = {'1 by 1': lambda d: d == 1, '1 by 2': lambda d: d == 2, '1 by 3+': lambda d: d >= 3,
              'X': lambda d: d == 0, '2 by 1': lambda d: d == -1, '2 by 2': lambda d: d == -2,
              '2 by 3+': lambda d: d <= -3}
        if label in mm:
            f = mm[label]
            r = build(lambda *s: f(F(*s)[0] - F(*s)[1]))
    elif base == 'Correct Score' and not side and not is_sub:
        pass  # 由调用方特殊处理（含 other）
    elif base == 'Clean Sheet' and not is_sub and side:
        # 规则: "Predict whether Home/Away team keeps a clean sheet at Full Time"
        # 主队零封 = 客队全场 0 球，客队零封 = 主队全场 0 球
        clean = build(lambda *s: (F(*s)[1] if side == 'H' else F(*s)[0]) == 0)
        if label == 'yes':
            r = clean
        elif label == 'no':
            r = FULL & ~clean
    elif base == 'Win To Nil' and not is_sub and side:
        cond = build(lambda *s: win(*s) and (F(*s)[1] if side == 'H' else F(*s)[0]) == 0)
        if label == 'yes':
            r = cond
        elif label == 'no':
            r = FULL & ~cond
    elif base == 'To Win' and not is_sub and side:
        cond = build(win)
        if label == 'yes':
            r = cond
        elif label == 'no':
            r = FULL & ~cond
    elif base == 'Which Team To Score' and not side and not is_sub:
        if label == 'none':
            r = build(lambda *s: total(*s) == 0)
        elif label == 'only 1':
            r = build(lambda *s: F(*s)[0] > 0 and F(*s)[1] == 0)
        elif label == 'only 2':
            r = build(lambda *s: F(*s)[0] == 0 and F(*s)[1] > 0)
        elif label == 'both teams':
            r = build(lambda *s: F(*s)[0] > 0 and F(*s)[1] > 0)

    # ---------- "X Or Y" 复合盘口 ----------
    # 官方规则: "Home wins OR over x.5"，No = "Home does not win AND under x.5"
    # 即 No 是 Yes 的补集。统一用 `_or_market` 构造。
    elif base in ('Or Over 2.5', 'Or Under 2.5',
                  'Or Both Teams To Score', 'Or Any Clean Sheet') and side and not is_sub:
        L = None
        if line:
            try:
                L = float(line)
            except ValueError:
                L = None
        yes_mask = _or_condition(base, side, L)
        if yes_mask is not None:
            if label == 'yes':
                r = yes_mask
            elif label == 'no':
                r = FULL & ~yes_mask

    elif base in ('Draw Or Over 2.5', 'Draw Or Under 2.5',
                  'Draw Or Both Teams To Score', 'Draw Or Any Clean Sheet') and not side and not is_sub:
        L = None
        if line:
            try:
                L = float(line)
            except ValueError:
                L = None
        yes_mask = _draw_or_condition(base, L)
        if yes_mask is not None:
            if label == 'yes':
                r = yes_mask
            elif label == 'no':
                r = FULL & ~yes_mask

    # ---------- 两半场类 ----------
    # "score more than opponent in BOTH halves"
    elif base == 'To Win Both Halves' and side and not is_sub:
        cond = build(lambda *s: _half_win(s, side, first=True)
                     and _half_win(s, side, first=False))
        if label == 'yes':
            r = cond
        elif label == 'no':
            r = FULL & ~cond
    # "score more than opponent in AT LEAST ONE half"
    elif base == 'To Win Either Half' and side and not is_sub:
        cond = build(lambda *s: _half_win(s, side, first=True)
                     or _half_win(s, side, first=False))
        if label == 'yes':
            r = cond
        elif label == 'no':
            r = FULL & ~cond
    # "scores in both halves"
    elif base == 'To Score In Both Halves' and side and not is_sub:
        cond = build(lambda *s: _half_goals(s, side, first=True) > 0
                     and _half_goals(s, side, first=False) > 0)
        if label == 'yes':
            r = cond
        elif label == 'no':
            r = FULL & ~cond
    elif base in ('Both Halves Over 1.5', 'Both Halves Under 1.5') and not side and not is_sub:
        L = float(line) if line else 1.5
        is_over = base.endswith('Over 1.5')
        cond = build(lambda *s: _half_total(s, first=True) > L
                     and _half_total(s, first=False) > L) if is_over else \
               build(lambda *s: _half_total(s, first=True) < L
                     and _half_total(s, first=False) < L)
        if label == 'yes':
            r = cond
        elif label == 'no':
            r = FULL & ~cond
    # "which half is higher scoring"（全场 / 队名）
    elif base == 'Highest Scoring Half' and not is_sub:
        if side:
            first_goals = lambda *s: _half_goals(s, side, first=True)
            second_goals = lambda *s: _half_goals(s, side, first=False)
        else:
            first_goals = lambda *s: _half_total(s, first=True)
            second_goals = lambda *s: _half_total(s, first=False)
        if label == '1st half':
            r = build(lambda *s: first_goals(*s) > second_goals(*s))
        elif label == '2nd half':
            r = build(lambda *s: first_goals(*s) < second_goals(*s))
        elif label == 'equal':
            r = build(lambda *s: first_goals(*s) == second_goals(*s))

    # ---------- Odd/Even（全场 / 队名） ----------
    elif base == 'Odd/Even' and not is_sub:
        fn = tg if side else total
        if label == 'odd':
            r = build(lambda *s: fn(*s) % 2 == 1)
        elif label == 'even':
            r = build(lambda *s: fn(*s) % 2 == 0)

    # ---------- To Win（队名）已有，此处不重复 ----------

    elif base == '1x2 & Both Teams To Score' and not side and not is_sub:
        if ' & ' in label:
            rr, bb = label.split(' & ')
            btts = (lambda *s: F(*s)[0] > 0 and F(*s)[1] > 0) if bb == 'yes' else \
                   (lambda *s: F(*s)[0] == 0 or F(*s)[1] == 0)
            r = build(lambda *s: res(*F(*s)) == rr and btts(*s))
    elif base == '1x2 & Total' and not side and not is_sub:
        if ' & ' in label and line:
            rr, ou = label.split(' & ')
            L = float(line)
            oo = (lambda *s: total(*s) > L) if ou == 'over' else (lambda *s: total(*s) < L)
            r = build(lambda *s: res(*F(*s)) == rr and oo(*s))
    elif base == 'Total & Both Teams To Score' and not side and not is_sub:
        try:
            part, bb = label.split(' & ')
            ou, L = part.split()
            L = float(L)
            oo = (lambda *s: total(*s) > L) if ou == 'over' else (lambda *s: total(*s) < L)
            btts = (lambda *s: F(*s)[0] > 0 and F(*s)[1] > 0) if bb == 'yes' else \
                   (lambda *s: F(*s)[0] == 0 or F(*s)[1] == 0)
            r = build(lambda *s: oo(*s) and btts(*s))
        except Exception:
            pass
    elif base == 'Double Chance & Total' and not side and not is_sub:
        if ' & ' in label and line:
            dc, ou = label.split(' & ')
            # 实际标签用斜杠写法（1/X、1/2、X/2），两种写法都要认
            ok = {'1X': '1X', '1/X': '1X', '1/2': '12', '12': '12',
                  'X/2': 'X2', 'X2': 'X2'}.get(dc)
            if ok:
                L = float(line)
                oo = (lambda *s: total(*s) > L) if ou == 'over' else (lambda *s: total(*s) < L)
                r = build(lambda *s: res(*F(*s)) in ok and oo(*s))
    elif base == 'Double Chance & Both Teams To Score' and not side and not is_sub:
        if ' & ' in label:
            dc, bb = label.split(' & ')
            ok = {'1/X': '1X', '1/2': '12', 'X/2': 'X2'}.get(dc)
            if ok:
                btts = (lambda *s: F(*s)[0] > 0 and F(*s)[1] > 0) if bb == 'yes' else \
                       (lambda *s: F(*s)[0] == 0 or F(*s)[1] == 0)
                r = build(lambda *s: res(*F(*s)) in ok and btts(*s))

    # ---------- 半场（1st Half / 2nd Half 共用；HF/htotal/htg 已按前缀切换） ----------
    elif base == '1x2' and not side:
        r = build(lambda *s: res(*HF(*s)) == label)
    elif base == 'Double Chance' and not side:
        # 标签可能是 '1X'/'X2'/'12' 或 '1/X'/'X/2'/'1/2'
        ok = {'1X': '1X', '1/X': '1X', '12': '12', '1/2': '12',
              'X2': 'X2', 'X/2': 'X2'}.get(label)
        if ok:
            r = build(lambda *s: res(*HF(*s)) in ok)
    elif base == 'Multigoals' and not side:
        # 半场总进球区间（htotal 已按 1st/2nd Half 切换）
        if label == 'no goal':
            r = build(lambda *s: htotal(*s) == 0)
        elif label.endswith('+'):
            kk = int(label[:-1])
            r = build(lambda *s: htotal(*s) >= kk)
        elif '-' in label:
            lo, hi = map(int, label.split('-'))
            r = build(lambda *s: lo <= htotal(*s) <= hi)
    elif base == 'Odd/Even' and not side:
        if label == 'odd':
            r = build(lambda *s: htotal(*s) % 2 == 1)
        elif label == 'even':
            r = build(lambda *s: htotal(*s) % 2 == 0)
    elif base == 'Total' and not side:
        L = float(line) if line else None
        if L is not None:
            if label == 'over':
                r = build(lambda *s: htotal(*s) > L)
            elif label == 'under':
                r = build(lambda *s: htotal(*s) < L)
    elif base == 'Total' and side:
        L = float(line) if line else None
        if L is not None:
            if label == 'over':
                r = build(lambda *s: htg(*s) > L)
            elif label == 'under':
                r = build(lambda *s: htg(*s) < L)
    elif base == 'Both Teams To Score' and not side:
        if label == 'yes':
            r = build(lambda *s: HF(*s)[0] > 0 and HF(*s)[1] > 0)
        elif label == 'no':
            r = build(lambda *s: HF(*s)[0] == 0 or HF(*s)[1] == 0)
    elif base == 'Handicap' and not side:
        hp = _h(line)
        if hp:
            hl, al = hp
            if label == '1':
                r = build(lambda *s: HF(*s)[0] + hl > HF(*s)[1] + al)
            elif label == 'X':
                r = build(lambda *s: HF(*s)[0] + hl == HF(*s)[1] + al)
            elif label == '2':
                r = build(lambda *s: HF(*s)[0] + hl < HF(*s)[1] + al)
    elif base == 'Exact Goals' and not side:
        if label.endswith('+'):
            kk = int(label[:-1])
            r = build(lambda *s: htotal(*s) >= kk)
        elif label.isdigit():
            kk = int(label)
            r = build(lambda *s: htotal(*s) == kk)
    elif base == '1x2 & Total' and not side:
        if ' & ' in label and line:
            rr, ou = label.split(' & ')
            L = float(line)
            oo = (lambda *s: htotal(*s) > L) if ou == 'over' else (lambda *s: htotal(*s) < L)
            r = build(lambda *s: res(*HF(*s)) == rr and oo(*s))
    elif base == '1x2 & Both Teams To Score' and not side:
        if ' & ' in label:
            rr, bb = label.split(' & ')
            btts = (lambda *s: HF(*s)[0] > 0 and HF(*s)[1] > 0) if bb == 'yes' else \
                   (lambda *s: HF(*s)[0] == 0 or HF(*s)[1] == 0)
            r = build(lambda *s: res(*HF(*s)) == rr and btts(*s))

    # ---------- 半场/全场 组合 ----------
    elif base == 'Halftime/Fulltime' and not side:
        if '/' in label:
            r1, r2 = label.split('/')
            r = build(lambda *s: res(*HF(*s)) == r1 and res(*F(*s)) == r2)
    elif base == 'Halftime/Fulltime & Total' and not side:
        if ' & ' in label:
            htft, ou = label.split(' & ')
            if '/' in htft and line:
                r1, r2 = htft.split('/')
                L = float(line)
                oo = (lambda *s: total(*s) > L) if ou == 'over' else (lambda *s: total(*s) < L)
                r = build(lambda *s: res(*HF(*s)) == r1 and res(*F(*s)) == r2 and oo(*s))
    elif base == 'Halftime/Fulltime & Exact Goals' and not side:
        if ' & ' in label:
            htft, g = label.split(' & ')
            if '/' in htft:
                r1, r2 = htft.split('/')
                if g.endswith('+'):
                    kk = int(g[:-1])
                    r = build(lambda *s: res(*HF(*s)) == r1 and res(*F(*s)) == r2 and total(*s) >= kk)
                elif g.isdigit():
                    kk = int(g)
                    r = build(lambda *s: res(*HF(*s)) == r1 and res(*F(*s)) == r2 and total(*s) == kk)
    elif base == 'Highest Scoring Half' and not side:
        if label == '1st half':
            r = build(lambda *s: htotal(*s) > total(*s) - htotal(*s))
        elif label == '2nd half':
            r = build(lambda *s: htotal(*s) < total(*s) - htotal(*s))
        elif label == 'equal':
            r = build(lambda *s: htotal(*s) == total(*s) - htotal(*s))
    elif base == '1st/2nd Half Both Teams To Score' and not side:
        if '/' in label:
            h1b, h2b = label.split('/')
            def pred(hh1, aa1, hh2, aa2, b1=h1b, b2=h2b):
                f1 = (hh1 > 0 and aa1 > 0) if b1 == 'yes' else (hh1 == 0 or aa1 == 0)
                s2h, s2a = hh2 - hh1, aa2 - aa1
                f2 = (s2h > 0 and s2a > 0) if b2 == 'yes' else (s2h == 0 or s2a == 0)
                return f1 and f2
            r = build(pred)

    _cache[k] = r
    return r


def collect(path, home, away):
    cands = []
    for mk in json.load(open(path, encoding='utf-8')):
        nm, sels = parse_mari(mk['mari'])
        if not nm:
            continue
        side, base = split_team(nm, home, away)
        named = 0
        other = None
        for line, lab, odds in sels:
            # 比分盘需要单独拼 other 补集，不走 covers()
            if base == 'Correct Score':
                if ':' in lab:
                    hh, aa = map(int, lab.split(':'))
                    if hh < G and aa < G:
                        m = build(lambda h1, a1, h2, a2, x=hh, y=aa: h2 == x and a2 == y)
                        named |= m
                        # 具名比分同时进候选池。原先只累入 named、不进池，
                        # 理由是「1/赔率 太大不会进最优解」—— 实测不成立：
                        # 高赔率具名可以补上低价大覆盖选项漏掉的窄缝，
                        # 以更小的增量补齐覆盖（真实场次 245064451588395947
                        # 由 1.085252 降到 1.066374）。候选池只增不减，
                        # 最优成本只会下降或持平，不会引入假套利。
                        cands.append((m, odds, f'{nm}|{line}|{lab}'))
                elif lab == 'other':
                    other = (odds, nm)
                continue
            if base == '1st Half - Correct Score':
                if ':' in lab:
                    hh, aa = map(int, lab.split(':'))
                    if hh < H and aa < H:
                        m = build(lambda h1, a1, h2, a2, x=hh, y=aa: h1 == x and a1 == y)
                        named |= m
                        cands.append((m, odds, f'{nm}|{line}|{lab}'))
                elif lab == 'other':
                    other = (odds, nm)
                continue
            m = covers(base, side, line, lab)
            if m is not None:
                cands.append((m, odds, f'{nm}|{line}|{lab}'))
        # named 为 0（盘口没列具名比分）时 other 就是全部状态，
        # FULL & ~0 = FULL 依然正确。原来的 `other and named` 会把
        # 这种情况整个丢掉，候选数归零、整场误判 NO_COVER。
        if base in ('Correct Score', '1st Half - Correct Score') and other is not None:
            cands.append((FULL & ~named, other[0], f'{other[1]}|other'))
    return cands

def collect_from_rows(rows):
    """从数据库行构造候选。

    rows: [(market, side, line, selection, odds), ...]
          market 是已归一化的盘口名（含半场前缀），side 已拆分。

    与 collect() 逻辑相同，只是数据源从文件换成数据库行。
    """
    cands = []
    by_market = {}
    for market, side, line, label, odds in rows:
        by_market.setdefault((market, side), []).append((line, label, odds))

    for (market, side), sels in by_market.items():
        named = 0
        other = None

        # 比分盘：具名比分与 other 补集都进候选池。
        # 具名曾被认为「1/赔率 太大，不会进最优解」而整类丢弃，实测不成立：
        # 高赔率具名可以补齐低价大覆盖选项漏掉的窄缝，用更小的增量完成覆盖。
        # 详见 docs/superpowers/specs/2026-11-04-correct-score-named-review.md
        if market in ('Correct Score', '1st Half - Correct Score'):
            is_half_cs = market.startswith('1st Half')
            limit = H if is_half_cs else G
            for line, label, odds in sels:
                if ':' in label:
                    try:
                        hh, aa = map(int, label.split(':'))
                    except ValueError:
                        continue
                    if hh < limit and aa < limit:
                        if is_half_cs:
                            m = build(lambda h1, a1, h2, a2, x=hh, y=aa:
                                      h1 == x and a1 == y)
                        else:
                            m = build(lambda h1, a1, h2, a2, x=hh, y=aa:
                                      h2 == x and a2 == y)
                        named |= m
                        cands.append((m, odds,
                                      f'{market}|{side}|{line}|{label}'))
                elif label == 'other':
                    other = odds
            # named 为 0 时 FULL & ~0 = FULL，other 即全部状态，依然正确。
            if other is not None:
                cands.append((FULL & ~named, other, f'{market}|other'))
            continue

        for line, label, odds in sels:
            m = covers(market, side, line, label)
            if m is not None:
                cands.append((m, odds, f'{market}|{side}|{line}|{label}'))

    return cands
