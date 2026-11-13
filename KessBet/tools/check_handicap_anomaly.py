"""定位「盘口内部和异常」的成因。

已知：6311 组三选一盘口里 67 组内部和 < 1，最低的全是 `1st Half - Handicap`。
问题：这些异常出现在什么时刻？半场结束后？还是比赛中途？

如果是「1st Half 盘口在半场结束后还挂着」，那成因就是：
平台不撤已结算的半场盘，只是把赔率改成了无意义的值。
这种情况下 `market_active()` 已经能在分析层挡住，
但采集层还会把垃圾存进库。

如果是比赛中途就异常，那就是别的原因。
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
        SELECT match_id, changed_time, market, side, line, selection, odds,
               minute, home_score, away_score
        FROM kessbet_odds_live_history
        WHERE changed_time > NOW() - INTERVAL 3 DAY
          AND market LIKE '%Handicap%'
          AND odds > 1
        """)
        rows = cur.fetchall()

        groups = defaultdict(list)
        meta = {}
        for mid, ts, mkt, side, line, sel, o, minute, hs, as_ in rows:
            groups[(mid, ts, mkt, side, line)].append(float(o))
            meta[(mid, ts, mkt, side, line)] = (minute, hs, as_)

        bad = []
        for k, v in groups.items():
            if len(v) >= 3:
                s = sum(1.0 / x for x in v)
                if s < 0.90:
                    bad.append((s, k, meta[k]))

        print('内部和 < 0.90 的三选一 Handicap 组: %d 个' % len(bad))
        print()
        print('%-8s %-18s %-22s %-6s %-8s %s'
              % ('match', '时间', '盘口|line', '分钟', '比分', 'Σ'))
        print('-' * 78)

        after_ht = 0
        for s, k, (minute, hs, as_) in sorted(bad):
            mkt = k[2]
            is_half_market = mkt.startswith('1st Half')
            over45 = minute is not None and minute > 45
            tag = ''
            if is_half_market and over45:
                tag = '  ← 半场盘 + 已过半场'
                after_ht += 1
            print('%-8s %-18s %-22s %-6s %-8s %.3f%s'
                  % (k[0][-6:], k[1].strftime('%m-%d %H:%M:%S'),
                     '%s|%s' % (mkt[:18], k[4]), minute,
                     '%s:%s' % (hs, as_), s, tag))

        print()
        print('其中「半场盘 + 已过半场」: %d / %d' % (after_ht, len(bad)))

        # 对照：正常的组在什么分钟
        normal_minutes = []
        for k, v in groups.items():
            if len(v) >= 3:
                s = sum(1.0 / x for x in v)
                if s >= 1.0 and meta[k][0] is not None:
                    normal_minutes.append(meta[k][0])
        if normal_minutes:
            normal_minutes.sort()
            n = len(normal_minutes)
            print('对照：正常组（Σ>=1）的分钟分布 min=%d 中位=%d max=%d'
                  % (normal_minutes[0], normal_minutes[n // 2],
                     normal_minutes[-1]))
    finally:
        dao.close()


if __name__ == '__main__':
    main()
