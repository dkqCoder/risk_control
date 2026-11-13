"""统计历史流水里「同一盘口内部和」的分布。

用来回答两个问题：
  1. 「单一博彩公司内部必然有抽水」这个前提成不成立？
     —— 如果成立，内部和应该几乎全部 >= 1，出现 < 1 的极少。
  2. 「阈值 0.90 会不会误伤」？
     —— 看真实分布里有多少组落在 0.90 以下。
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.dao import Dao


def main():
    dao = Dao()
    cur = dao.conn.cursor()
    try:
        cur.execute("""
        SELECT match_id, changed_time, market, side, line, selection, odds
        FROM kessbet_odds_live_history
        WHERE changed_time > NOW() - INTERVAL 3 DAY
          AND market IN ('Handicap', '1x2', 'Total', 'Double Chance',
                         '1st Half - Handicap', '1st Half - Total')
          AND odds > 1
        """)
        rows = cur.fetchall()
        print('扫描 %d 行历史流水' % len(rows))

        groups = defaultdict(list)
        for mid, ts, mkt, side, line, sel, o in rows:
            groups[(mid, ts, mkt, side, line)].append(float(o))

        # 只统计「至少 3 个选项」的组 —— 2 个选项的 Total 类本来就该是
        # Σ≈1.07，不影响；但 1 个选项的组没有意义。
        sums = []
        for k, v in groups.items():
            if len(v) >= 3:
                sums.append((sum(1.0 / x for x in v), k))
        sums.sort()

        print('三选一盘口共 %d 组' % len(sums))
        if not sums:
            return
        vals = sorted(s[0] for s in sums)
        n = len(vals)
        print('内部和分布: min=%.3f  p1=%.3f  中位=%.3f  p99=%.3f  max=%.3f'
              % (vals[0], vals[n // 100], vals[n // 2],
                 vals[99 * n // 100], vals[-1]))
        for th in (0.5, 0.8, 0.9, 0.95, 1.0):
            c = sum(1 for v in vals if v < th)
            print('  < %.2f : %5d 组 (%.2f%%)' % (th, c, 100.0 * c / n))

        print()
        print('最低的 8 组:')
        for s, k in sums[:8]:
            print('  Σ=%.3f  %s %s  %-20s %s|%s'
                  % (s, k[0][-6:], k[1], k[2][:20], k[3] or '-', k[4] or '-'))
    finally:
        dao.close()


if __name__ == '__main__':
    main()
