"""清理「盘线串门」bug 产生的误报命中快照。

## 判据

误报的特征：下注组合里的盘线是一串选项 ID，正常应该是 `1.5` / `0:1` / 空。

    Halftime/Fulltime & Total||2451694162941421|X/2 & under
                                ↑ 应该是 1.5 / 2.5 / 3.5

成因：`parse_mari` 里盘线名丢失后，兄弟选项被算到上一条盘线头上
（或反过来把选项 ID 当盘线）。已在 2026-11-13 修复。

## 做什么

扫 `kessbet_arbitrage_snapshot`，把 ilp_chosen / lp_chosen 里含
「长数字盘线」的记录删掉。默认只打印不删，加 --apply 才真删。

用法：
    py tools/clean_bad_snapshots.py            # 只看
    py tools/clean_bad_snapshots.py --apply    # 真删
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.dao import Dao

# desc 形如 `market|side|line|label`；line 是 8 位以上纯数字 = 选项 ID 被当成盘线
_BAD_LINE = re.compile(r'\|(\d{8,})\|')


def is_bad(*jsons):
    for j in jsons:
        if not j:
            continue
        try:
            data = json.loads(j)
        except Exception:
            data = j
        text = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
        if _BAD_LINE.search(text):
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='真的删除')
    ap.add_argument('--cost-below', type=float, default=None, metavar='X',
                    help='同时删除成本 < X 的记录（滚球误报用这个判据）')
    args = ap.parse_args()

    dao = Dao()
    try:
        with dao.conn.cursor() as c:
            c.execute("""
            SELECT id, alert_time, match_id, home_team, away_team,
                   cost, is_live, ilp_chosen, lp_chosen
            FROM kessbet_arbitrage_snapshot
            ORDER BY alert_time
            """)
            rows = c.fetchall()

        def _hit(r):
            if is_bad(r[7], r[8]):
                return '盘线异常'
            if args.cost_below is not None and float(r[5]) < args.cost_below:
                return '成本异常'
            return None

        bad = [(r, _hit(r)) for r in rows]
        bad = [(r, why) for r, why in bad if why]
        print('命中记录总数 %d，其中可疑的 %d 条' % (len(rows), len(bad)))
        print()
        for r, why in bad:
            print('  id=%-4s %s  %-18s vs %-18s  cost=%.4f  live=%s  %s'
                  % (r[0], r[1].strftime('%m-%d %H:%M'),
                     (r[3] or '')[:18], (r[4] or '')[:18],
                     float(r[5]), r[6], why))

        if not bad:
            return
        if not args.apply:
            print()
            print('（只预览。加 --apply 才会真的删除）')
            return

        ids = [r[0] for r, _ in bad]
        ph = ','.join(['%s'] * len(ids))
        with dao.conn.cursor() as c:
            c.execute(f"DELETE FROM kessbet_arbitrage_snapshot "
                      f"WHERE id IN ({ph})", ids)
            print()
            print('已删除 %d 条' % c.rowcount)
        dao.conn.commit()

        with dao.conn.cursor() as c:
            c.execute("SELECT COUNT(*) FROM kessbet_arbitrage_snapshot")
            print('表内剩余 %d 条' % c.fetchone()[0])
    finally:
        dao.close()


if __name__ == '__main__':
    main()
