"""analyze/payout.py — 收益矩阵与资金配置 LP。

两种模型并存：
  solver.solve()      覆盖模型（ILP）：选哪几个选项，成本 = Σ(1/赔率)
  payout.lp_solve()   资金配置（LP）：每个选项投多少钱，最差状态净收益最大

两者在"选项只能全买或全不买"时等价（实测 80 场零差异），
但 LP 更贴近现实，也能容纳未来的非二元结算（走盘/半赢）。
"""
import logging

import numpy as np
import scipy.sparse as sp
from scipy.optimize import linprog

log = logging.getLogger(__name__)

# 赔率下限：<= 该值视为停售（平台标记位为 0），不能下注
MIN_ODDS = 1.0

# 判定阈值：最差状态净收益必须严格大于该值才算套利
# 不能用 0，因为求解器有数值误差；也不能太大，否则漏掉真实机会
ARB_EPS = 1e-6


class PayoutResult:
    __slots__ = ('status', 'worst_profit', 'stakes', 'chosen',
                 'state_profits', 'worst_states', 'error')

    def __init__(self, status, worst_profit=None, stakes=None, chosen=None,
                 state_profits=None, worst_states=None, error=None):
        self.status = status
        self.worst_profit = worst_profit
        self.stakes = stakes or []
        self.chosen = chosen or []
        self.state_profits = state_profits
        self.worst_states = worst_states or []
        self.error = error

    @property
    def is_arbitrage(self) -> bool:
        return (self.status == 'ok'
                and self.worst_profit is not None
                and self.worst_profit > ARB_EPS)

    def __repr__(self):
        return (f'PayoutResult(status={self.status}, '
                f'worst={self.worst_profit}, chosen={len(self.chosen)})')


def profit_matrix(candidates, n_states: int) -> tuple[np.ndarray, list]:
    """把 (bitmask, 赔率, 描述) 候选转成收益矩阵。

    返回 (G, kept)：
      G[i, r]  = 投注 1 元到选项 i，在状态 r 下的**净收益**
               = 赔率 - 1（中奖）或 -1（输）
      kept     = 保留的候选列表（已过滤停售和无覆盖）

    停售选项（赔率 <= 1.0）被排除——平台标记位为 0，下了不成立。
    """
    kept = [(m, o, d) for m, o, d in candidates if m and o > MIN_ODDS]
    n = len(kept)
    if n == 0:
        return np.zeros((0, n_states)), []

    # 稀疏构造：默认 -1（输），覆盖到的状态改成 odds-1
    G = np.full((n, n_states), -1.0, dtype=np.float64)
    for i, (mask, odds, _) in enumerate(kept):
        m = mask
        while m:
            low = m & -m
            r = low.bit_length() - 1
            G[i, r] = odds - 1.0
            m ^= low

    return G, kept


def lp_solve(candidates, n_states: int,
             stake_total: float = 1.0) -> PayoutResult:
    """资金配置 LP：在总投注额固定的前提下，最大化最差状态的净收益。

        max  t
        s.t. Σ s_i·G[i,r] >= t      对每个状态 r
             Σ s_i = stake_total
             s_i >= 0
             t 无约束

    G[i,r] 是单位投注的净收益（见 profit_matrix）。

    t > 0 表示存在严格套利：无论什么结果都赚钱。
    t = 0 表示最多保本。
    t < 0 表示无论怎么分配都会亏（正常情况，就是平台抽水）。
    """
    G, kept = profit_matrix(candidates, n_states)
    n = G.shape[0]
    if n == 0:
        return PayoutResult('no_candidates',
                            error='过滤停售后没有可用候选')

    # 变量 [s_1..s_n, t]，共 n+1
    # 目标：max t  ->  min -t
    c = np.zeros(n + 1)
    c[-1] = -1.0

    # 约束：-G.T @ s + t <= 0   （即 Σ s_i·G[i,r] - t >= 0）
    A = np.hstack([-G.T, np.ones((n_states, 1))])
    b = np.zeros(n_states)

    # 等式约束：Σ s_i = stake_total
    A_eq = np.zeros((1, n + 1))
    A_eq[0, :n] = 1.0
    b_eq = np.array([stake_total])

    # 边界：s_i >= 0，t 自由（-inf, +inf）
    bounds = [(0, None)] * n + [(None, None)]

    try:
        res = linprog(c=c, A_ub=A, b_ub=b, A_eq=A_eq, b_eq=b_eq,
                      bounds=bounds, method='highs')
    except Exception as e:
        log.exception('lp_solve failed: %s', e)
        return PayoutResult('error', error=str(e))

    if not res.success:
        return PayoutResult('infeasible', error=res.message)

    s = res.x[:n]
    worst = float(res.x[n])

    # 独立复算：不信任求解器返回的 t，重新算每个状态的净收益
    profits = G.T @ s
    worst = float(profits.min())
    worst_states = [int(i) for i in np.flatnonzero(profits < worst + 1e-9)]

    chosen = [(kept[i][2], float(s[i])) for i in range(n) if s[i] > 1e-9]
    chosen.sort(key=lambda x: -x[1])

    return PayoutResult('ok', worst_profit=worst, stakes=s.tolist(),
                        chosen=chosen, state_profits=profits.tolist(),
                        worst_states=worst_states)


def verify_payout(result: PayoutResult, candidates, n_states: int,
                  stake_total: float = 1.0) -> dict:
    """独立复算：给定归一化投注额，重算每种状态的实际收益。

    不依赖求解器返回的任何数值，只用 G 矩阵和 s 向量重新计算。
    """
    G, kept = profit_matrix(candidates, n_states)
    n = G.shape[0]

    desc_to_idx = {d: i for i, (_, _, d) in enumerate(kept)}
    s = np.zeros(n)
    for desc, stake in result.chosen:
        i = desc_to_idx.get(desc)
        if i is not None:
            s[i] = stake

    total_stake = s.sum()
    profits = G.T @ s
    worst = float(profits.min())
    best = float(profits.max())

    return {
        'total_stake': float(total_stake),
        'worst_profit': worst,
        'best_profit': best,
        'worst_state': int(profits.argmin()),
        'n_states_loss': int((profits < 0).sum()),
        'n_states_profit': int((profits > 0).sum()),
        'matches_total': abs(total_stake - stake_total) < 1e-6,
        'is_profitable': worst > ARB_EPS,
    }
