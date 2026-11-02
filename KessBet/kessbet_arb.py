"""半场+全场双维度套利扫描原型。

状态空间: (h1, a1, h2, a2) 其中
  h1,a1 = 半场主客进球 (0..H-1)
  h2,a2 = 全场主客进球 (0..G-1)
约束: h2>=h1 且 a2>=a1 （全场进球数不可能少于半场）

覆盖成本 = sum(1/赔率) < 1 即为无损套利。

解析函数已抽到 collect.kessbet.parse，这里 re-export 以保持向后兼容。
"""
import json, glob, os, itertools, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect.kessbet.parse import parse_mari, split_team   # noqa: F401

# 状态空间与盘口语义已抽到 analyze.covers.soccer，这里 re-export 保持兼容
from analyze.covers.soccer import (   # noqa: F401
    H, G, STATES, NS, IDX, FULL,
    build, covers, collect,
)


def solve(cands):
    """向后兼容包装：委托给 analyze.solver.solve。

    返回 (cost, chosen, gap_state)，与旧接口一致。
    """
    from analyze.solver import solve as _solve, NO_COVER
    r = _solve(cands, NS)
    if r.status == NO_COVER:
        return None, [], r.gap_state
    if r.cost is None:
        return None, [], None
    return r.cost, r.chosen, None


if __name__ == '__main__':
    DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
    raw = json.load(open(os.path.join(DATA, 'kessbet_soccer_raw.json'), encoding='utf-8'))
    info = {}
    for g in raw['data']['leagueGroupList']:
        for m in g['matchList']:
            info[m['matchId']] = m
    print(f'状态数 {NS}')
    n = 0
    for mid in info:
        p = os.path.join(DATA, 'odds', 'raw_odds', f'{mid}.json')
        if not os.path.exists(p):
            continue
        h, a = info[mid]['htn'], info[mid]['atn']
        cs = collect(p, h, a)
        cost, ch, gap = solve(cs)
        if gap is not None:
            print(f'  {h} vs {a}: 状态 {gap} 无覆盖')
            continue
        print(f'{cost:.4f}  {h[:16]:16} vs {a[:16]:16}  选项{len(cs):>3}  选中{len(ch)}')
        n += 1
        if n >= 5:
            break
