"""analyze/live/verify.py — 独立的白名单无界结算验证（D02）。

## 为什么不复用 soccer.STATES

有限状态空间（每队 0..9 球）会把 10:0 当成不存在。用同一个 bitmask 再验
一遍只是重复证明同一个漏洞。这里用**独立的结算规则**直接计算每个终局的
返还，并在无界比分域上找亏损反例。

## 白名单

当前认证：

- 常规时间全场 1x2 / Double Chance / Both Teams To Score；
- 常规时间全场 Total（半球线，`x.5`）和球队 Total（半球线）。
- 全场 Clean Sheet、Exact Goals、具名 Correct Score、1x2 与双方进球或
  半球线总进球的组合。
- 全场 Win To Nil、To Win、Which Team To Score。
- 三向整数让球与全场/球队进球奇偶盘。
- 全场/球队 Multigoals、全场 Goal Range 与 Winning Margin。
- 全场 Total & Both Teams To Score、Double Chance & Total、
  Double Chance & Both Teams To Score。
- 球队获胜或指定条件成立，以及平局或指定条件成立的全场 Or 盘。

Correct Score 的 `other`、半场盘及其他尚未认证的组合盘
仍返回 `unsupported_rules`。

## 为什么有限枚举是完备的

上述选项的结算只依赖四件事：

1. 结果：h > a / h == a / h < a；
2. 是否为零：h == 0、a == 0；
3. 与盘线或精确进球阈值比较：h、a 或 h+a；
4. 具名正确比分只在指定的 `(h,a)` 上成立；让球与胜差盘取决于比分差；
5. 奇偶盘取决于相应进球数模 2。

半球线 L = k + 0.5 时，整数比分下 `x > L ⇔ x >= k+1`、`x < L ⇔ x <= k`。
所以所有谓词只在 `0` 和各盘线的 `k / k+1` 处改变。

取 `B = max(所有阈值) + 2`：

- 枚举 `h,a ∈ [0,B]` 覆盖所有阈值组合；
- 补一行 `h=B+1, a∈[0,B]` 和一列 `a=B+1, h∈[0,B]`；
- 再补尾部代表 `(B+2,B+1)`、`(B+1,B+1)`、`(B+1,B+2)`，覆盖两轴都
  超过 B 时 h<a / h==a / h>a 三种情况（此时所有盘线谓词已恒定）。

让球盘额外枚举其比分差阈值附近；奇偶盘额外枚举高比分两种余数。
这些代表点覆盖无界非负整数域上的结算等价区域。新增白名单市场时
必须重新检查完备性论证。
"""
from __future__ import annotations

from decimal import Decimal
from math import floor

WHITELIST_VERSION = 'wt-v8'

_FULL_TIME_SIMPLE = {'1x2', 'Double Chance', 'Both Teams To Score'}
_GOAL_RANGES = {
    '0-1': (0, 1), '2-3': (2, 3), '4-5': (4, 5),
    '4-6': (4, 6), '6+': (6, None), '7+': (7, None),
}
_MARGINS = {'1 by 1', '1 by 2', '1 by 3+', 'X',
            '2 by 1', '2 by 2', '2 by 3+'}
_DC = {'1X': '1X', '1/X': '1X', '12': '12', '1/2': '12',
       'X2': 'X2', 'X/2': 'X2'}
_OR_MARKETS = {
    'Or Over 2.5', 'Or Under 2.5', 'Or Both Teams To Score',
    'Or Any Clean Sheet', 'Draw Or Over 2.5', 'Draw Or Under 2.5',
    'Draw Or Both Teams To Score', 'Draw Or Any Clean Sheet',
}


def _half_line(line):
    try:
        value = float(line)
    except (TypeError, ValueError):
        return False
    return abs(value - floor(value) - 0.5) < 1e-9


def _goal_band(label):
    if label == 'no goal':
        return 0, 0
    if label.endswith('+') and label[:-1].isdigit():
        return int(label[:-1]), None
    parts = label.split('-')
    if len(parts) == 2 and all(x.isdigit() for x in parts):
        lo, hi = map(int, parts)
        if lo <= hi:
            return lo, hi
    return None


def _parse(desc: str):
    """把 `market|side|line|label` 拆成四元组；不支持的形态返回 None。"""
    p = str(desc).split('|')
    if len(p) != 4:
        return None
    return p[0], p[1], p[2], p[3]


def is_whitelisted(market: str, side: str, line: str, label: str) -> bool:
    if market in _FULL_TIME_SIMPLE:
        return side == '' and line == ''
    if market == 'Clean Sheet':
        return side in ('H', 'A') and line == '' and label in ('yes', 'no')
    if market in ('Win To Nil', 'To Win'):
        return side in ('H', 'A') and line == '' and label in ('yes', 'no')
    if market == 'Which Team To Score':
        return side == '' and line == '' and label in (
            'none', 'only 1', 'only 2', 'both teams')
    if market in _OR_MARKETS:
        if label not in ('yes', 'no'):
            return False
        if side not in (('',) if market.startswith('Draw ') else ('H', 'A')):
            return False
        return (line == '2.5' if ('Over 2.5' in market or 'Under 2.5' in market)
                else line == '')
    if market == 'Handicap':
        parts = line.split(':')
        return (side == '' and label in ('1', 'X', '2') and
                len(parts) == 2 and all(x.lstrip('-').isdigit() for x in parts))
    if market == 'Odd/Even':
        return side in ('', 'H', 'A') and line == '' and label in ('odd', 'even')
    if market == 'Multigoals':
        return side in ('', 'H', 'A') and line == '' and _goal_band(label) is not None
    if market == 'Goal Range':
        return side == '' and line == '' and label in _GOAL_RANGES
    if market == 'Winning Margin':
        return side == '' and line == '' and label in _MARGINS
    if market == 'Exact Goals':
        return (side in ('', 'H', 'A') and line == '' and
                (label.isdigit() or (label.endswith('+') and label[:-1].isdigit())))
    if market == 'Correct Score':
        if side != '' or line != '' or label == 'other':
            return False
        parts = label.split(':')
        return len(parts) == 2 and all(x.isdigit() for x in parts)
    if market in ('1x2 & Both Teams To Score', '1x2 & Total'):
        parts = label.split(' & ')
        if side != '' or len(parts) != 2 or parts[0] not in ('1', 'X', '2'):
            return False
        if market == '1x2 & Both Teams To Score':
            return line == '' and parts[1] in ('yes', 'no')
        try:
            L = float(line)
        except (TypeError, ValueError):
            return False
        return parts[1] in ('over', 'under') and abs(L - floor(L) - 0.5) < 1e-9
    if market == 'Total & Both Teams To Score':
        parts = label.split(' & ')
        if side != '' or line != '' or len(parts) != 2 or parts[1] not in ('yes', 'no'):
            return False
        total = parts[0].split()
        return (len(total) == 2 and total[0] in ('over', 'under')
                and _half_line(total[1]))
    if market in ('Double Chance & Total', 'Double Chance & Both Teams To Score'):
        parts = label.split(' & ')
        if side != '' or len(parts) != 2 or parts[0] not in _DC:
            return False
        if market == 'Double Chance & Total':
            return _half_line(line) and parts[1] in ('over', 'under')
        return line == '' and parts[1] in ('yes', 'no')
    if market == 'Total':
        if side not in ('', 'H', 'A'):
            return False
        try:
            L = float(line)
        except (TypeError, ValueError):
            return False
        return abs(L - floor(L) - 0.5) < 1e-9   # 只认半球线
    return False


def _pred(sel, h: int, a: int) -> bool | None:
    """单腿是否结算为赢。返回 None 表示规则不支持。"""
    market, side, line, label = sel
    if market == '1x2':
        if label == '1':
            return h > a
        if label == 'X':
            return h == a
        if label == '2':
            return h < a
        return None
    if market == 'Double Chance':
        if label == '1X':
            return h >= a
        if label == '12':
            return h != a
        if label == 'X2':
            return h <= a
        return None
    if market == 'Both Teams To Score':
        if label == 'yes':
            return h > 0 and a > 0
        if label == 'no':
            return h == 0 or a == 0
        return None
    if market == 'Total':
        L = float(line)
        v = h + a if side == '' else (h if side == 'H' else a)
        if label == 'over':
            return v > L
        if label == 'under':
            return v < L
        return None
    if market == 'Clean Sheet':
        clean = (a == 0) if side == 'H' else (h == 0)
        return clean if label == 'yes' else not clean
    if market in ('Win To Nil', 'To Win'):
        wins = h > a if side == 'H' else a > h
        cond = wins
        if market == 'Win To Nil':
            cond = wins and ((a == 0) if side == 'H' else (h == 0))
        return cond if label == 'yes' else not cond
    if market == 'Which Team To Score':
        return {
            'none': h == 0 and a == 0,
            'only 1': h > 0 and a == 0,
            'only 2': h == 0 and a > 0,
            'both teams': h > 0 and a > 0,
        }[label]
    if market in _OR_MARKETS:
        base = h == a if market.startswith('Draw ') else (
            h > a if side == 'H' else a > h)
        if 'Over 2.5' in market:
            other = h + a > 2.5
        elif 'Under 2.5' in market:
            other = h + a < 2.5
        elif 'Both Teams To Score' in market:
            other = h > 0 and a > 0
        else:
            other = h == 0 or a == 0
        yes = base or other
        return yes if label == 'yes' else not yes
    if market == 'Handicap':
        hl, al = map(int, line.split(':'))
        diff = h + hl - a - al
        return diff > 0 if label == '1' else (diff == 0 if label == 'X' else diff < 0)
    if market == 'Odd/Even':
        goals = h + a if side == '' else (h if side == 'H' else a)
        return goals % 2 == (1 if label == 'odd' else 0)
    if market in ('Multigoals', 'Goal Range'):
        goals = h + a if side == '' else (h if side == 'H' else a)
        lo, hi = (_goal_band(label) if market == 'Multigoals'
                  else _GOAL_RANGES[label])
        return goals >= lo and (hi is None or goals <= hi)
    if market == 'Winning Margin':
        diff = h - a
        return {
            '1 by 1': diff == 1, '1 by 2': diff == 2,
            '1 by 3+': diff >= 3, 'X': diff == 0,
            '2 by 1': diff == -1, '2 by 2': diff == -2,
            '2 by 3+': diff <= -3,
        }[label]
    if market == 'Exact Goals':
        v = h + a if side == '' else (h if side == 'H' else a)
        if label.endswith('+'):
            return v >= int(label[:-1])
        return v == int(label)
    if market == 'Correct Score':
        hh, aa = map(int, label.split(':'))
        return h == hh and a == aa
    if market in ('1x2 & Both Teams To Score', '1x2 & Total'):
        result, condition = label.split(' & ')
        actual = '1' if h > a else ('X' if h == a else '2')
        if market == '1x2 & Both Teams To Score':
            other = (h > 0 and a > 0)
            return actual == result and (other if condition == 'yes' else not other)
        other = (h + a > float(line))
        return actual == result and (other if condition == 'over' else not other)
    if market in ('Total & Both Teams To Score', 'Double Chance & Total',
                  'Double Chance & Both Teams To Score'):
        first, second = label.split(' & ')
        if market == 'Total & Both Teams To Score':
            direction, threshold = first.split()
            total = h + a > float(threshold)
            return (total if direction == 'over' else not total) and (
                (h > 0 and a > 0) if second == 'yes' else (h == 0 or a == 0))
        actual = '1' if h > a else ('X' if h == a else '2')
        if actual not in _DC[first]:
            return False
        if market == 'Double Chance & Total':
            total = h + a > float(line)
            return total if second == 'over' else not total
        both = h > 0 and a > 0
        return both if second == 'yes' else not both
    return None


def _thresholds(selections) -> list[int]:
    vals = {0, 1}
    for market, _side, line, _label in selections:
        if market in ('Total', '1x2 & Total', 'Double Chance & Total'):
            k = int(floor(float(line)))
            vals.add(k)
            vals.add(k + 1)
        elif market in _OR_MARKETS and ('Over 2.5' in market or 'Under 2.5' in market):
            vals.update((2, 3))
        elif market == 'Total & Both Teams To Score':
            k = int(floor(float(_label.split(' & ')[0].split()[1])))
            vals.update((k, k + 1))
        elif market == 'Exact Goals':
            k = int(_label.rstrip('+'))
            vals.update((k, k + 1))
        elif market == 'Correct Score':
            hh, aa = map(int, _label.split(':'))
            vals.update((hh, hh + 1, aa, aa + 1))
        elif market in ('Multigoals', 'Goal Range'):
            lo, hi = (_goal_band(_label) if market == 'Multigoals'
                      else _GOAL_RANGES[_label])
            vals.update((lo, lo + 1))
            if hi is not None:
                vals.update((hi, hi + 1))
    return sorted(vals)


def _points(selections, home_score: int = 0,
            away_score: int = 0) -> list[tuple[int, int]]:
    vals = _thresholds(selections)
    margins = [abs(int(line.split(':')[1]) - int(line.split(':')[0]))
               for market, _side, line, _label in selections
               if market == 'Handicap']
    if any(market == 'Winning Margin' for market, *_ in selections):
        margins.append(3)
    # 上界必须同时覆盖盘线阈值和当前比分。只按盘线取上界时，7:0 且只有
    # 1x2（没有 Total 线）会生成不到 7 的代表点，误报 no_reachable_point。
    D = max(margins, default=0)
    B = max(max(vals), home_score, away_score, D) + 2
    pts = [(h, a) for h in range(0, B + 1) for a in range(0, B + 1)]
    pts += [(B + 1, a) for a in range(0, B + 1)]
    pts += [(h, B + 1) for h in range(0, B + 1)]
    # 两队都超过 B 时，比分差仍会决定三项让球盘。为每个可能跨过
    # 让球阈值的差值留代表点，不能只检查 -1/0/1。
    pts += [(B + 1 + d, B + 1) for d in range(D + 2)]
    pts += [(B + 1, B + 1 + d) for d in range(1, D + 2)]
    if any(market == 'Odd/Even' for market, _side, _line, _label in selections):
        # 奇偶在无界尾部不会稳定。单轴超界检查连续两种奇偶；双轴
        # 超界同时覆盖比分差阈值和基数奇偶。
        for high in range(B + 2, B + D + 5):
            pts.extend((high, low) for low in range(B + 1))
            pts.extend((low, high) for low in range(B + 1))
        for base in (B + 1, B + 2):
            for diff in range(-D - 3, D + 4):
                pts.append((base + max(diff, 0),
                            base + max(-diff, 0)))
    return pts, B


def _allocate_units(weights, units_total: int) -> list[int]:
    """把预算单位按权重分配，确定性处理余数（最大余数法）。"""
    raw = [w * units_total for w in weights]
    units = [int(x) for x in raw]
    rem = units_total - sum(units)
    order = sorted(range(len(raw)),
                   key=lambda i: (-(raw[i] - units[i]), i))
    for i in order[:rem]:
        units[i] += 1
    return units


def verify_chosen(chosen, home_score: int, away_score: int,
                  ht_home: int | None = None,
                  ht_away: int | None = None,
                  total_stake: float = 10000.0,
                  stake_unit: float = 1.0,
                  min_profit: float = 0.0) -> dict:
    """验证 chosen 组合在无界比分域、按实际金额取整后的最低返还。

    chosen: [(desc, odds), ...]
    返回 {'status', 'min_payout', 'actual_total', 'stakes',
          'counterexample', 'cells', 'reason'}。
    status:
      verified             每个可达终局的返还都严格高于总投入，且达到门槛
      counterexample_found  找到亏损/不达标的终局
      unsupported_rules    有非白名单选项，不能认证
      invalid_input        赔率/预算非法、空组合
    """
    sels = []
    for desc, odds in chosen:
        p = _parse(desc)
        if p is None:
            return {'status': 'unsupported_rules', 'reason': f'bad_desc:{desc}',
                    'min_payout': None, 'counterexample': None,
                    'cells': 0, 'whitelist_version': WHITELIST_VERSION}
        if not is_whitelisted(*p):
            return {'status': 'unsupported_rules',
                    'reason': f'not_whitelisted:{desc}',
                    'min_payout': None, 'counterexample': None,
                    'cells': 0, 'whitelist_version': WHITELIST_VERSION}
        try:
            o = float(odds)
        except (TypeError, ValueError):
            o = 0.0
        if not (o > 1.0):
            return {'status': 'invalid_input', 'reason': f'bad_odds:{odds}',
                    'min_payout': None, 'counterexample': None,
                    'cells': 0, 'whitelist_version': WHITELIST_VERSION}
        sels.append((p, o))

    if not sels:
        return {'status': 'invalid_input', 'reason': 'empty',
                'min_payout': None, 'counterexample': None,
                'cells': 0, 'whitelist_version': WHITELIST_VERSION}

    try:
        total_dec = Decimal(str(total_stake))
        unit_dec = Decimal(str(stake_unit))
        profit_dec = Decimal(str(min_profit))
    except Exception:
        return {'status': 'invalid_input', 'reason': 'bad_budget',
                'min_payout': None, 'counterexample': None,
                'cells': 0, 'whitelist_version': WHITELIST_VERSION}
    if (unit_dec <= 0 or total_dec <= 0
            or total_dec % unit_dec != 0):
        return {'status': 'invalid_input',
                'reason': 'budget_not_divisible_by_unit',
                'min_payout': None, 'counterexample': None,
                'cells': 0, 'whitelist_version': WHITELIST_VERSION}
    units_total = int(total_dec / unit_dec)
    if units_total <= 0:
        return {'status': 'invalid_input', 'reason': 'zero_budget',
                'min_payout': None, 'counterexample': None,
                'cells': 0, 'whitelist_version': WHITELIST_VERSION}

    inv = [Decimal(1) / Decimal(str(o)) for _, o in sels]
    total_inv = sum(inv)
    weights = [x / total_inv for x in inv]
    units = _allocate_units(weights, units_total)
    stakes = [{'desc': '|'.join(p), 'odds': o,
               'units': u, 'amount': float(unit_dec * u)}
              for (p, o), u in zip(sels, units)]
    actual_total = unit_dec * units_total
    need = actual_total * (Decimal('1') + profit_dec)
    eps = Decimal('1e-9')

    min_h = max(home_score, ht_home if ht_home is not None else 0)
    min_a = max(away_score, ht_away if ht_away is not None else 0)

    pts, B = _points([p for p, _ in sels], home_score, away_score)
    worst = None
    checked = 0
    for h, a in pts:
        if h < min_h or a < min_a:
            continue
        checked += 1
        payout = Decimal('0')
        legs = []
        for (p, o), u in zip(sels, units):
            win = _pred(p, h, a)
            if win is None:
                return {'status': 'unsupported_rules',
                        'reason': f'bad_label:{p}',
                        'min_payout': None, 'counterexample': None,
                        'cells': checked,
                        'whitelist_version': WHITELIST_VERSION}
            ret = (unit_dec * u * Decimal(str(o))) if win else Decimal('0')
            payout += ret
            legs.append({'desc': '|'.join(p), 'win': bool(win),
                         'return': float(ret), 'amount': float(unit_dec * u)})
        if worst is None or payout < worst['_payout']:
            worst = {'home_score': h, 'away_score': a,
                     'payout': float(payout), '_payout': payout,
                     'legs': legs}

    if worst is None:
        return {'status': 'invalid_input', 'reason': 'no_reachable_point',
                'min_payout': None, 'counterexample': None,
                'cells': 0, 'whitelist_version': WHITELIST_VERSION}

    counterexample = {k: v for k, v in worst.items() if k != '_payout'}
    if worst['_payout'] <= actual_total + eps or worst['_payout'] < need - eps:
        return {'status': 'counterexample_found',
                'min_payout': worst['payout'],
                'actual_total': float(actual_total),
                'stakes': stakes,
                'counterexample': counterexample,
                'cells': checked, 'bound': B,
                'total_stake': float(total_dec),
                'stake_unit': float(unit_dec), 'min_profit': min_profit,
                'whitelist_version': WHITELIST_VERSION}
    return {'status': 'verified', 'min_payout': worst['payout'],
            'actual_total': float(actual_total),
            'stakes': stakes,
            'counterexample': None, 'cells': checked, 'bound': B,
            'total_stake': float(total_dec),
            'stake_unit': float(unit_dec), 'min_profit': min_profit,
            'whitelist_version': WHITELIST_VERSION}
