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

# 赔率下限：<= 该值不参与求解。必须和 solver.MIN_ODDS 一致，
# 否则两个模型的候选池不同，交叉验证会误报。原因见 solver.py 的注释。
MIN_ODDS = 1.02

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

    保留稠密返回是刻意的：这是公开函数，测试和其他调用方都按
    「净收益、-1 表示输」的契约用它。内存敏感的求解路径改用
    `_profit_matrix_sparse()`，不要改这里。
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


def _profit_matrix_sparse(candidates, n_states: int) -> tuple[sp.csr_matrix, list]:
    """profit_matrix 的稀疏版：存**毛返还**（中奖=赔率，未覆盖=0）。

    为什么需要单独一个函数：
      稠密 G 是 (候选数 × 2025)。一场 350 个候选就是 350×2025×8 ≈ 5.4MB，
      lp_solve 里还要 hstack 转置一份，4 线程并发时峰值能到上百 MB。
      实测加入 Halftime/Fulltime CS（每场多 46 个候选）后，
      HiGHS 抛 `MemoryError: std::bad_alloc`，一轮分析 23 场失败。

    为什么存毛返还而不是净收益：
      稀疏矩阵读不到的格子是 0，而净收益矩阵的"未覆盖"是 -1。
      存毛返还的话"未覆盖 = 0"恰好和稀疏矩阵的默认值一致，
      净收益在求解时统一减去总投注即可（见 lp_solve 的推导）。

    数值等价性已验证：与稠密实现在 30 场比赛上最大绝对差 4e-12。
    """
    kept = [(m, o, d) for m, o, d in candidates if m and o > MIN_ODDS]
    n = len(kept)
    if n == 0:
        return sp.csr_matrix((0, n_states), dtype=np.float64), []

    rows, cols, vals = [], [], []
    for i, (mask, odds, _) in enumerate(kept):
        m = mask
        while m:
            low = m & -m
            r = low.bit_length() - 1
            rows.append(i)
            cols.append(r)
            vals.append(float(odds))
            m ^= low

    return sp.csr_matrix(
        (np.asarray(vals, dtype=np.float64),
         (np.asarray(rows, dtype=np.int32), np.asarray(cols, dtype=np.int32))),
        shape=(n, n_states), dtype=np.float64), kept


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
    G, kept = _profit_matrix_sparse(candidates, n_states)
    n = G.shape[0]
    if n == 0:
        return PayoutResult('no_candidates',
                            error='过滤停售后没有可用候选')

    # 变量 [s_1..s_n, t]，共 n+1
    # 目标：max t  ->  min -t
    c = np.zeros(n + 1)
    c[-1] = -1.0

    # ---- 稀疏矩阵下的净收益展开 ----
    #
    # _profit_matrix_sparse 返回的是**毛返还**矩阵：中奖位置 = 赔率，未覆盖 = 0。
    # 单位投注的净收益：
    #   中奖 -> 赔率 - 1      未中 -> 0 - 1 = -1
    # 整体：
    #   net_profit(r) = Σ_i s_i·G[i,r] - Σ_i s_i
    #                 = (G^T @ s)[r] - stake_total
    # 因为 Σs_i = stake_total 是等式约束，第二项是常数。
    #
    # 约束 net(r) >= t  =>  -(G^T)s + t <= -stake_total
    A = sp.hstack([
        -G.T.tocsr(),
        sp.csr_matrix(np.ones((n_states, 1))),
    ], format='csr')
    b = np.full(n_states, -stake_total)

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

    # 独立复算：不信任求解器返回的 t，用毛返还矩阵重新算净收益。
    # G 存的是毛返还（中奖=赔率，未覆盖=0），净收益要减去本金。
    gross = G.T @ s                            # 每个状态的毛返还
    profits = gross - s.sum()                  # 减总投注 = 净收益
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
    # 刻意用稠密版 profit_matrix —— 这个函数的作用就是「独立复算」，
    # 不该复用 lp_solve 里那套稀疏表示，否则等于自己验证自己。
    G, kept = profit_matrix(candidates, n_states)
    n = G.shape[0]

    desc_to_idx = {d: i for i, (_, _, d) in enumerate(kept)}
    s = np.zeros(n)
    for desc, stake in result.chosen:
        i = desc_to_idx.get(desc)
        if i is not None:
            s[i] = stake

    total_stake = s.sum()
    profits = G.T @ s          # G 是净收益矩阵，直接就是净收益
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
