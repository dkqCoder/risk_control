"""解析器修复后，重算全部赛前成本并对比变化。

## 背景

2026-11-13 修了 parse_mari 的「盘线串门」bug：某条盘线的第一个选项
被锁定或赔率非法时，盘线名丢失，后续选项被算到上一条盘线头上。
后果是同一条线下出现重复标签、内部和异常（实测 0.2~0.5），
进而和别的盘口组合出假套利。

这个 bug 同时影响赛前和滚球。库里已有的成本是用旧解析器算的，
需要重算一遍看影响面。

## 做法

1. 把当前 kessbet_match_cost 的成本存成快照
2. 强制全量重算
3. 逐场对比新旧成本，输出变化最显著的

用法：
    py tools/recalc_after_parse_fix.py --limit 200
    py tools/recalc_after_parse_fix.py --minutes 60   # 只看 60 分钟内开赛的
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyze.run import analyze_once
from db.dao import Dao


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=400, help='最多重算多少场')
    ap.add_argument('--minutes', type=int, default=0,
                    help='只看 N 分钟内开赛的，0=用默认 horizon')
    ap.add_argument('--concurrency', type=int, default=6)
    args = ap.parse_args()

    dao = Dao()

    # 1) 快照旧成本
    print('读取当前成本快照...', flush=True)
    with dao.conn.cursor() as c:
        c.execute("""
        SELECT c.match_id, c.cost, c.n_selections, c.n_candidates
        FROM kessbet_match_cost c
        JOIN kessbet_match m ON m.platform=c.platform AND m.match_id=c.match_id
        WHERE c.platform='kessbet' AND c.cost IS NOT NULL
          AND m.kickoff_time > NOW()
          AND m.sport='sr:sport:1'
        ORDER BY m.kickoff_time
        LIMIT %s
        """, (args.limit,))
        old = {r[0]: {'cost': float(r[1]), 'n_sel': r[2], 'n_cand': r[3]}
               for r in c.fetchall()}
    print('待重算 %d 场' % len(old), flush=True)
    if not old:
        return

    # 2) 强制重算
    horizon = None
    if args.minutes:
        horizon = max(1, args.minutes // 60)
    print('开始重算（force=True，concurrency=%d）...' % args.concurrency,
          flush=True)
    t0 = time.time()
    stats = analyze_once(dao, horizon_hours=horizon, force=True,
                         concurrency=args.concurrency)
    print('重算完成 %.0fs: %s' % (time.time() - t0, stats), flush=True)

    # 3) 读新成本并对比
    print('对比新旧成本...', flush=True)
    ids = list(old)
    ph = ','.join(['%s'] * len(ids))
    with dao.conn.cursor() as c:
        c.execute(f"""
        SELECT match_id, cost, n_selections, n_candidates, error
        FROM kessbet_match_cost
        WHERE platform='kessbet' AND match_id IN ({ph})
        """, ids)
        new = {r[0]: {'cost': float(r[1]) if r[1] is not None else None,
                      'n_sel': r[2], 'n_cand': r[3], 'error': r[4]}
               for r in c.fetchall()}

    rows = []
    for mid, o in old.items():
        n = new.get(mid)
        if not n:
            continue
        nc = n['cost']
        if nc is None:
            rows.append((None, mid, o['cost'], None, o['n_sel'], n['n_sel'],
                         n.get('error')))
            continue
        diff = nc - o['cost']
        rows.append((diff, mid, o['cost'], nc, o['n_sel'], n['n_sel'],
                     n.get('error')))

    changed = [r for r in rows if r[0] is not None and abs(r[0]) > 1e-6]
    became_null = [r for r in rows if r[0] is None]
    up = [r for r in changed if r[0] > 0]
    down = [r for r in changed if r[0] < 0]

    print()
    print('=' * 78)
    print('共对比 %d 场' % len(rows))
    print('  成本变化 > 1e-6: %d 场' % len(changed))
    print('    上升（旧解析低估了）: %d 场' % len(up))
    print('    下降: %d 场' % len(down))
    print('  重算后无法覆盖: %d 场' % len(became_null))

    if up:
        print()
        print('成本上升最多的 10 场（旧解析串门导致低估）:')
        print('  %-10s %-10s %-10s %-8s %s'
              % ('match', '旧成本', '新成本', '变化', '选项数 旧→新'))
        for diff, mid, oc, nc, os_, ns, err in sorted(up, reverse=True)[:10]:
            print('  %-10s %-10.4f %-10.4f %-+8.4f %s→%s'
                  % (mid[-8:], oc, nc, diff, os_, ns))

    if down:
        print()
        print('成本下降的（解析修正后找到更优覆盖）:')
        for diff, mid, oc, nc, os_, ns, err in sorted(down)[:5]:
            print('  %-10s %-10.4f %-10.4f %-+8.4f'
                  % (mid[-8:], oc, nc, diff))

    dao.close()


if __name__ == '__main__':
    main()
