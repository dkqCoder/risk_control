"""回填 kessbet_match_cost_history — 让列表页的「1h 变化」立刻有数据。

为什么需要一次性回填：
  这张表由 analyze 在成本变化时增量追加。刚上线时它是空的，
  「1h 变化」列会全是「—」，要等足够多的成本变化自然发生才陆续有数。
  而赔率流水（kessbet_odds_history）是现成的 —— 对每个选项取
  「1 小时前的赔率」，重算一遍成本，就能把过去这段时间的变化补上。

一次性操作，之后由 analyze 增量维护。重复执行安全：
唯一键 (platform, match_id, odds_updated) 保证不会写重复行。

用法:
    py tools/backfill_cost_history.py                  # 回填未来 72h 内的比赛
    py tools/backfill_cost_history.py --hours 6        # 只回填未来 6h 的
    py tools/backfill_cost_history.py --limit 50       # 最多 50 场（先试试水）
    py tools/backfill_cost_history.py --dry-run        # 只报告，不写库

注意：每场要跑一遍 ILP，约 1.5 秒。500 场就是十几分钟，请耐心等。
"""
import argparse
import os
import sys
import time
from datetime import timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from analyze.covers.soccer import NS, collect_from_rows  # noqa: E402
from analyze import solver                               # noqa: E402
from common.timeutil import now_beijing                  # noqa: E402
from db.dao import Dao                                   # noqa: E402

PLATFORM = 'kessbet'
SPORT = 'sr:sport:1'


def odds_one_hour_ago(cur, match_id: str, minutes: int = 60):
    """对每个当前选项，取 N 分钟前最后一次出现的赔率。

    历史里没有记录的选项（N 分钟前还没上架）沿用当前赔率 ——
    这会让「1 小时前」偏向当前值，但比直接丢弃这个选项好：
    丢了会造成覆盖缺口、整场算成 NO_COVER。
    """
    cur.execute(f"""
        SELECT p.market, p.side, p.line, p.selection,
               COALESCE(
                 (SELECT h.odds FROM kessbet_odds_history h
                  WHERE h.platform=p.platform AND h.match_id=p.match_id
                    AND h.market=p.market AND h.side=p.side
                    AND h.line=p.line AND h.selection=p.selection
                    AND h.changed_time <= NOW() - INTERVAL %s MINUTE
                  ORDER BY h.changed_time DESC LIMIT 1),
                 p.odds) AS odds_prev
        FROM kessbet_odds_prematch p
        WHERE p.platform=%s AND p.match_id=%s
    """, (minutes, PLATFORM, match_id))
    return [(r[0], r[1], r[2], r[3], float(r[4])) for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser(description='回填成本变动流水')
    ap.add_argument('--hours', type=int, default=72,
                    help='只处理未来 N 小时内开赛的比赛（默认 72）')
    ap.add_argument('--limit', type=int, default=0,
                    help='最多处理多少场，0 表示不限')
    ap.add_argument('--minutes', type=int, default=60,
                    help='回溯多少分钟，默认 60（即 1h 变化）')
    ap.add_argument('--dry-run', action='store_true', help='只报告不写库')
    args = ap.parse_args()

    dao = Dao()
    try:
        with dao.conn.cursor() as cur:
            cur.execute("""
                SELECT m.match_id, MAX(m.home_team), MAX(m.away_team)
                FROM kessbet_match m
                WHERE m.platform=%s AND m.sport=%s
                  AND m.kickoff_time BETWEEN NOW() AND NOW() + INTERVAL %s HOUR
                GROUP BY m.match_id
                ORDER BY MIN(m.kickoff_time)
            """, (PLATFORM, SPORT, args.hours))
            matches = cur.fetchall()

        if args.limit:
            matches = matches[:args.limit]

        print(f'待处理 {len(matches)} 场（未来 {args.hours}h 内开赛）')
        if args.dry_run:
            print('--dry-run：不写库')

        written = skipped = failed = 0
        t0 = time.time()
        for i, (mid, home, away) in enumerate(matches, 1):
            try:
                with dao.conn.cursor() as cur:
                    prev_rows = odds_one_hour_ago(cur, mid, args.minutes)
                    cur.execute("""SELECT market, side, line, selection, odds
                                   FROM kessbet_odds_prematch
                                   WHERE platform=%s AND match_id=%s""",
                                (PLATFORM, mid))
                    now_rows = [(r[0], r[1], r[2], r[3], float(r[4]))
                                for r in cur.fetchall()]
                    cur.execute("""SELECT MAX(updated_time)
                                   FROM kessbet_odds_prematch
                                   WHERE platform=%s AND match_id=%s""",
                                (PLATFORM, mid))
                    odds_updated = cur.fetchone()[0]

                r_prev = solver.solve(collect_from_rows(prev_rows), NS)
                if r_prev.cost is None:
                    skipped += 1
                    continue

                if not args.dry_run:
                    # changed_time 必须回拨到回溯的那个时刻 —— 这一行代表的是
                    # 「N 分钟前成本是多少」，不是「现在写了一条记录」。
                    # 写成 now 的话，cost_at(minutes=N) 永远查不到它
                    # （条件要求 changed_time <= NOW() - N 分钟）。
                    dao.append_cost_history(
                        platform=PLATFORM, match_id=mid, cost=r_prev.cost,
                        prev_cost=None, odds_updated=odds_updated,
                        changed_time=now_beijing() - timedelta(
                            minutes=args.minutes),
                    )
                    dao.conn.commit()
                written += 1

                if i % 20 == 0:
                    el = time.time() - t0
                    print(f'  {i}/{len(matches)}  已写 {written}  '
                          f'跳过 {skipped}  失败 {failed}  '
                          f'({el:.0f}s, {el/i:.2f}s/场)')
            except Exception as e:
                failed += 1
                print(f'  [!] {mid} {home} vs {away}: {type(e).__name__}: {e}')
                try:
                    dao.conn.rollback()
                except Exception:
                    pass

        el = time.time() - t0
        print(f'\n完成：写 {written}  跳过 {skipped}  失败 {failed}  '
              f'耗时 {el:.0f}s ({el / max(len(matches), 1):.2f}s/场)')
        if written and not args.dry_run:
            print('现在列表页的「1h 变化」应该有数据了。')
    finally:
        dao.close()


if __name__ == '__main__':
    main()
