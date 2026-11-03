"""analyze/solver.py — 最小覆盖成本求解（运动无关）。

问题：给定一组候选（每个候选覆盖状态空间的一个子集，成本 = 1/赔率），
求一组候选覆盖所有状态且总成本最小。

    min  Σ costᵢ · xᵢ            xᵢ ∈ {0,1}
    s.t. 对每个状态 s: Σ xᵢ ≥ 1  （i 覆盖 s）

**必须用精确解（ILP），不能用贪心。** 贪心会高估约 0.33
（实测 1.35 vs 真值 1.02），因为大覆盖面会挤掉后续更优的小选项。

运动无关：状态是什么含义、有多少个，都由调用方决定。

## 为什么不用 pulp

之前用 pulp.PULP_CBC_CMD，它每次求解起一个 cbc.exe 子进程。Windows 上
连续跑约 100 次后，子进程启动返回 0xC0000142 (STATUS_DLL_INIT_FAILED)，
之后永久失败——桌面堆耗尽。

改用 scipy.optimize.milp（HiGHS，进程内求解）：200 次连续求解零失败，
且更快（0.16s vs 0.34s）。
"""
import logging

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
import scipy.sparse as sp

log = logging.getLogger(__name__)

# 结果状态
OK = 'ok'
NO_COVER = 'no_cover'          # 存在状态没有任何候选能覆盖
INFEASIBLE = 'infeasible'      # ILP 无解（理论上不应出现）
SOLVER_ERROR = 'solver_error'

# 赔率下限：<= 该值不参与求解。
#
# 为什么不是 1.0：
#   平台上停售选项记作 1.00，但 1.01~1.02 的选项也是「极端盘线」——
#   比如「客队上半场进 <2.5 球」「两队都赢下半场 no」这种几乎必然成立的项。
#   它们的 1/赔率 ≈ 0.99，看起来能极便宜地覆盖大片状态，于是 ILP 会优先选它们，
#   算出一个漂亮的成本。
#
#   实测 245159490624721227：
#     不过滤        cost=1.0568  选中 1st Half-Total A 2.5 under (1.01)
#     MIN_ODDS>1.02 cost=1.0815  回到正常的 1x2 + Double Chance
#   那 0.025 的「便宜」是假的 —— 这种赔率现实中有限注、随时停售，
#   收益 0.01 也没有操作意义。
#
#   全库统计：赔率 1.00~1.02 只有 223 个选项，且把门槛从 1.00 提到 1.05，
#   21 场样本的最优成本**完全不变** —— 说明这类选项本来就进不了正常解，
#   只有个别场次会因为它们产生虚低成本。
MIN_ODDS = 1.02


class SolveResult:
    __slots__ = ('cost', 'chosen', 'status', 'gap_state')

    def __init__(self, cost=None, chosen=None, status=OK, gap_state=None):
        self.cost = cost
        self.chosen = chosen or []
        self.status = status
        self.gap_state = gap_state

    def __repr__(self):
        return (f'SolveResult(cost={self.cost}, chosen={len(self.chosen)}, '
                f'status={self.status})')


def solve(candidates, n_states: int, min_odds: float = MIN_ODDS) -> SolveResult:
    """求最小覆盖成本。

    candidates: [(bitmask, odds, description), ...]
                bitmask 的第 i 位为 1 表示该候选覆盖状态 i；
                odds 是十进制赔率，成本 = 1/odds。
    n_states:   状态总数。
    min_odds:   赔率下限。低于等于它的选项视为停售，直接排除。

    返回 SolveResult。cost < 1 表示存在无损套利。
    """
    # 过滤停售选项：赔率 1.00 的选项在平台上不能下注（mari 标记位为 0）。
    # 它们的 1/odds = 1.0，看起来"正好覆盖成本"，但参与求解会选出
    # 不存在的市场，算出偏低的成本。实测有场比赛因此低估 0.013。
    pool = [(m, 1.0 / o, i) for i, (m, o, _) in enumerate(candidates)
            if m and o > min_odds]
    if not pool:
        return SolveResult(status=NO_COVER, gap_state=0)

    n_var = len(pool)

    # 构造稀疏覆盖矩阵：每个候选覆盖哪些状态
    rows, cols = [], []
    covered_states = 0
    for col, (mask, _, _) in enumerate(pool):
        # 逐位扫描 mask，收集覆盖的状态下标
        m = mask
        while m:
            low = m & -m
            state = low.bit_length() - 1
            rows.append(state)
            cols.append(col)
            m ^= low
        covered_states += bin(mask).count('1')

    if not rows:
        return SolveResult(status=NO_COVER, gap_state=0)

    rows_arr = np.asarray(rows, dtype=np.int32)
    cols_arr = np.asarray(cols, dtype=np.int32)
    A = sp.csr_matrix((np.ones(len(rows_arr), dtype=np.float64),
                       (rows_arr, cols_arr)),
                      shape=(n_states, n_var))

    # 检查是否有状态无人覆盖
    per_state = np.asarray(A.sum(axis=1)).ravel()
    if per_state.min() < 1:
        gap = int(np.flatnonzero(per_state < 1)[0])
        return SolveResult(status=NO_COVER, gap_state=gap)

    cost = np.asarray([c for _, c, _ in pool], dtype=np.float64)

    try:
        res = milp(
            c=cost,
            constraints=LinearConstraint(A, lb=1.0, ub=np.inf),
            integrality=np.ones(n_var),
            bounds=Bounds(0, 1),
        )
    except Exception as e:
        log.warning('solver failed: %s', e)
        return SolveResult(status=SOLVER_ERROR)

    if res.status == 2:                     # infeasible
        return SolveResult(status=INFEASIBLE)
    if res.status != 0 or res.x is None:    # 非最优
        log.warning('solver status=%s msg=%s', res.status, res.message)
        return SolveResult(status=SOLVER_ERROR)

    chosen = [candidates[pool[i][2]][2]
              for i in range(n_var) if res.x[i] > 0.5]
    return SolveResult(cost=float(res.fun), chosen=chosen)


def verify_coverage(candidates, chosen_descs, n_states: int) -> bool:
    """校验选中的组合确实覆盖了全部状态。用于自检。"""
    if not chosen_descs:
        return False
    chosen_set = set(chosen_descs)
    union = 0
    for mask, _, desc in candidates:
        if desc in chosen_set:
            union |= mask
    return union == (1 << n_states) - 1
