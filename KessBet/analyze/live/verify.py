"""analyze/live/verify.py — 独立的白名单无界结算验证（D02）。

## 为什么不复用 soccer.STATES

有限状态空间（每队 0..9 球）会把 10:0 当成不存在。用同一个 bitmask 再验
一遍只是重复证明同一个漏洞。这里用**独立的结算规则**直接计算每个终局的
返还，并在无界比分域上找亏损反例。

## 白名单

首期只认证：

- 常规时间全场 1x2 / Double Chance / Both Teams To Score；
- 常规时间全场 Total（半球线，`x.5`）和球队 Total（半球线）。

比分盘、半场盘、让球、奇偶、组合盘一律 `unsupported_rules`。

## 为什么有限枚举是完备的

上述选项的结算只依赖三件事：

1. 结果：h > a / h == a / h < a；
2. 是否为零：h == 0、a == 0；
3. 与盘线比较：h、a 或 h+a 相对于每个半球线 L。

半球线 L = k + 0.5 时，整数比分下 `x > L ⇔ x >= k+1`、`x < L ⇔ x <= k`。
所以所有谓词只在 `0` 和各盘线的 `k / k+1` 处改变。

取 `B = max(所有阈值) + 2`：

- 枚举 `h,a ∈ [0,B]` 覆盖所有阈值组合；
- 补一行 `h=B+1, a∈[0,B]` 和一列 `a=B+1, h∈[0,B]`；
- 再补尾部代表 `(B+2,B+1)`、`(B+1,B+1)`、`(B+1,B+2)`，覆盖两轴都
  超过 B 时 h<a / h==a / h>a 三种情况（此时所有盘线谓词已恒定）。

这组代表点覆盖了无界非负整数域上的所有结算等价区域，不是“随便取个
上限枚举”。新增白名单市场时必须重新检查这个完备性论证。
"""
from __future__ import annotations

from decimal import Decimal
from math import floor

WHITELIST_VERSION = 'wt-v1'

_FULL_TIME_SIMPLE = {'1x2', 'Double Chance', 'Both Teams To Score'}


def _parse(desc: str):
    """把 `market|side|line|label` 拆成四元组；不支持的形态返回 None。"""
    p = str(desc).split('|')
    if len(p) != 4:
        return None
    return p[0], p[1], p[2], p[3]


def is_whitelisted(market: str, side: str, line: str, label: str) -> bool:
    if market in _FULL_TIME_SIMPLE:
        return side == '' and line == ''
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
    return None


def _thresholds(selections) -> list[int]:
    vals = {0, 1}
    for market, _side, line, _label in selections:
        if market == 'Total':
            k = int(floor(float(line)))
            vals.add(k)
            vals.add(k + 1)
    return sorted(vals)


def _points(selections, home_score: int = 0,
            away_score: int = 0) -> list[tuple[int, int]]:
    vals = _thresholds(selections)
    # 上界必须同时覆盖盘线阈值和当前比分。只按盘线取上界时，7:0 且只有
    # 1x2（没有 Total 线）会生成不到 7 的代表点，误报 no_reachable_point。
    B = max(max(vals), home_score, away_score) + 2
    pts = [(h, a) for h in range(0, B + 1) for a in range(0, B + 1)]
    pts += [(B + 1, a) for a in range(0, B + 1)]
    pts += [(h, B + 1) for h in range(0, B + 1)]
    pts += [(B + 2, B + 1), (B + 1, B + 1), (B + 1, B + 2)]
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
