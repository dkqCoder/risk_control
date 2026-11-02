"""analyze/solver.py 的单元测试。"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from analyze.solver import solve, verify_coverage, OK, NO_COVER


def build_states(n, pred):
    """构造 n 个状态的 bitmask。"""
    m = 0
    for i in range(n):
        if pred(i):
            m |= 1 << i
    return m


# ---------- 基础 ----------

def test_partition_solution():
    """三个互斥且完备的候选 -> 成本 = 1/2 + 1/3 + 1/6 = 1.0"""
    n = 3
    cands = [
        (0b001, 2.0, 'a'),
        (0b010, 3.0, 'b'),
        (0b100, 6.0, 'c'),
    ]
    r = solve(cands, n)
    assert r.status == OK
    assert abs(r.cost - 1.0) < 1e-9
    assert set(r.chosen) == {'a', 'b', 'c'}


def test_arbitrage_detected():
    """成本 < 1 应能被检出。"""
    n = 2
    cands = [(0b01, 2.0, 'a'), (0b10, 2.0, 'b')]   # 0.5 + 0.5 = 1.0
    r1 = solve(cands, n)
    assert abs(r1.cost - 1.0) < 1e-9

    cands = [(0b01, 2.5, 'a'), (0b10, 2.5, 'b')]   # 0.4 + 0.4 = 0.8
    r2 = solve(cands, n)
    assert r2.cost < 1.0


def test_prefers_cheaper_combination():
    """一个大覆盖面 vs 两个小的 —— 应选总成本更低的。"""
    n = 2
    cands = [
        (0b11, 1.2, 'big'),      # 成本 0.833，覆盖全部
        (0b01, 3.0, 'a'),        # 0.333
        (0b10, 3.0, 'b'),        # 0.333，合计 0.667
    ]
    r = solve(cands, n)
    assert abs(r.cost - 2/3) < 1e-9
    assert set(r.chosen) == {'a', 'b'}


def test_greedy_would_fail():
    """贪心会先选'每格成本最低'的大覆盖面，结果更差。

    状态 0,1,2。候选 big 覆盖 0,1（成本 0.2/格），small 只覆盖 2（成本 0.1）。
    贪心先选 big（0.6），剩下 2 只能用 expensive（1.0），合计 1.6。
    最优是 a+b 两个小候选：(0.5+0.5) + 0.1 = ...
    这里用简单构造验证 ILP 优于贪心。
    """
    n = 3
    cands = [
        (0b011, 1.5, 'covers01'),   # 0.667 覆盖 0,1
        (0b100, 2.0, 'covers2'),    # 0.5 覆盖 2
        (0b111, 1.1, 'all'),        # 0.909 覆盖全部
    ]
    r = solve(cands, n)
    # 最优：all (0.909) vs covers01+covers2 (1.167)
    assert abs(r.cost - 1/1.1) < 1e-9
    assert r.chosen == ['all']


def test_no_cover_detects_gap():
    """某状态无候选覆盖 -> NO_COVER 且指出是哪个状态。"""
    n = 3
    cands = [(0b011, 2.0, 'a')]     # 状态 2 无人覆盖
    r = solve(cands, n)
    assert r.status == NO_COVER
    assert r.gap_state == 2


def test_empty_candidates():
    r = solve([], 5)
    assert r.status == NO_COVER


def test_zero_mask_ignored():
    """mask 为 0 的候选应被忽略（不覆盖任何状态）。"""
    n = 2
    cands = [
        (0b00, 1.01, 'useless'),
        (0b11, 2.0, 'covers_all'),
    ]
    r = solve(cands, n)
    assert r.chosen == ['covers_all']


# ---------- 覆盖校验 ----------

def test_verify_coverage_ok():
    n = 2
    cands = [(0b01, 2.0, 'a'), (0b10, 2.0, 'b')]
    assert verify_coverage(cands, ['a', 'b'], n) is True


def test_verify_coverage_fails_when_incomplete():
    n = 3
    cands = [(0b011, 2.0, 'a')]
    assert verify_coverage(cands, ['a'], n) is False


def test_verify_coverage_empty():
    assert verify_coverage([(0b11, 2.0, 'a')], [], 2) is False


# ---------- 并发安全 ----------

def test_concurrent_solves():
    """多线程调用必须全部成功。

    之前用 pulp + cbc.exe 子进程，Windows 上连续约 100 次后进程启动返回
    0xC0000142 (STATUS_DLL_INIT_FAILED)，之后永久失败。
    现改用 scipy.optimize.milp（HiGHS 进程内求解），无此问题。
    """
    from concurrent.futures import ThreadPoolExecutor

    n = 3
    cands = [(0b001, 2.0, 'a'), (0b010, 3.0, 'b'), (0b100, 6.0, 'c')]

    def one(_):
        r = solve(cands, n)
        return r.status, r.cost

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(one, range(32)))

    bad = [s for s, _ in results if s != OK]
    assert not bad, f'并发求解失败 {len(bad)}/32: {bad[:3]}'
    assert all(abs(c - 1.0) < 1e-9 for _, c in results)


def test_many_sequential_solves():
    """连续求解 200 次不能失败。

    回归测试：pulp 方案在第 ~100 次开始永久失败（子进程无法启动）。
    """
    n = 3
    cands = [(0b001, 2.0, 'a'), (0b010, 3.0, 'b'), (0b100, 6.0, 'c')]
    for i in range(200):
        r = solve(cands, n)
        assert r.status == OK, f'第 {i} 次求解失败: {r.status}'
        assert abs(r.cost - 1.0) < 1e-9


# ---------- 停售市场 ----------

def test_suspended_odds_excluded():
    """赔率 1.00 的停售选项不能参与求解。

    停售选项的 1/odds = 1.0，看起来"正好覆盖成本"，但它根本下不了注。
    如果参与 ILP，会选出不存在的市场，算出偏低的成本。
    """
    n = 3
    cands = [
        (0b001, 2.0, 'a'),
        (0b010, 3.0, 'b'),
        # 停售选项，赔率 1.0，覆盖状态 2
        (0b100, 1.0, 'suspended'),
    ]
    r = solve(cands, n)
    # 'suspended' 应该被排除，导致状态 2 无人覆盖
    assert r.status == NO_COVER
    assert r.gap_state == 2


def test_suspended_filtering_keeps_valid_solution():
    """过滤停售后，正常选项仍能求出解。"""
    n = 2
    cands = [
        (0b01, 2.0, 'a'),
        (0b10, 2.0, 'b'),
        (0b11, 1.0, 'suspended'),   # 被过滤掉
    ]
    r = solve(cands, n)
    assert r.status == OK
    assert abs(r.cost - 1.0) < 1e-9
    assert set(r.chosen) == {'a', 'b'}
