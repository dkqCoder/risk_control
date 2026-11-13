"""对比「平台原始 mari 串」和「我们解析入库的值」。

## 要回答的问题

2026-11-13 发现滚球命中里 168/183 是假的，典型形态：

    1st Half - Handicap 0:2
      1 @75.0   2 @85.0   X @12.0      Σ = 0.108

三选一盘口加总 0.108 在数学上不可能。两种可能：

  A. 平台真的返回了这种报价（停盘占位价之类）
  B. 我们解析错了（把不同盘线的选项混到一个 line 下）

这个脚本把原始串和解析结果并排打出来，直接看是哪种。

## 判据

对每个盘口，比较：
  1. 原始串里该盘口出现了几条盘线、每条线有哪些选项
  2. 解析后归到同一个 (market, side, line) 下的选项
  只要两者对不上，就是解析问题（B）。

用法：
    py tools/probe_live_raw_odds.py              # 扫当前所有滚球比赛
    py tools/probe_live_raw_odds.py --minutes     # 持续监测，发现异常就打印
"""
import argparse
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collect.kessbet.adapter import KessbetAdapter
from collect.kessbet.parse import parse_mari, split_team
from db.dao import Dao

# 内部和低于这个值就当成「可疑」打印出来
SUSPECT_SUM = 0.95


def dump_match(adapter, dao, meta):
    """检查一场比赛，返回可疑盘口列表。"""
    mid = meta['match_id']
    home = meta.get('home_team') or ''
    away = meta.get('away_team') or ''

    raw = adapter.fetch_raw(mid)
    if not raw:
        return None

    # 原始串 -> (market, side, line) -> {label: odds}
    parsed = defaultdict(dict)
    raw_by_market = defaultdict(list)

    for mk in raw:
        mari = mk.get('mari') or ''
        name, sels = parse_mari(mari)
        if not name:
            continue
        side, base = split_team(name, home, away)
        raw_by_market[(base, side)].append(mari)
        for line, label, odds in sels:
            key = (base, side, line)
            if label in parsed[key]:
                # 同一盘线下出现重复标签 —— 解析层就已经不自洽
                parsed[key][label + '#dup'] = odds
            parsed[key][label] = odds

    suspects = []
    for key, sels in parsed.items():
        total = sum(1.0 / o for o in sels.values() if o > 0)
        has_dup = any('#dup' in k for k in sels)
        if total < SUSPECT_SUM or has_dup:
            suspects.append((key, sels, total, raw_by_market.get(key[:2], [])))

    return {
        'mid': mid, 'home': home, 'away': away,
        'n_raw': len(raw), 'n_markets': len(parsed),
        'suspects': suspects,
    }


def show(info):
    print('=' * 78)
    print('%s vs %s   (matchId %s)' % (info['home'][:28], info['away'][:28],
                                       info['mid']))
    print('平台返回 %d 个盘口，解析出 %d 个 (market, side, line) 组'
          % (info['n_raw'], info['n_markets']))
    if not info['suspects']:
        print('  ✓ 未发现内部和异常的盘口')
        print()
        return

    for (market, side, line), sels, total, raws in info['suspects']:
        has_dup = any('#dup' in k for k in sels)
        print()
        print('  ⚠ %s | %s | %s    Σ = %.4f%s' %
              (market, side or '-', line or '-', total,
               '   ★ 同一盘线出现重复选项' if has_dup else ''))
        print('     解析出的选项:')
        for lab, o in sorted(sels.items()):
            print('        %-10s @%-10s   1/赔率=%.4f' % (lab, o, 1.0 / o))
        print('     原始串（该盘口）:')
        for r in raws:
            print('       %s' % r)
    print()


def scan_upcoming(adapter, dao, limit):
    """扫未开赛的比赛。

    **同一套解析器**。如果这些比赛没有内部和异常，说明解析逻辑本身
    是对的 —— 那滚球里出现的异常就是滚球特有的（平台行为或滚球盘口形态）。
    如果这里也能扫出异常，那就是解析 bug，和滚球无关。
    """
    with dao.conn.cursor() as c:
        c.execute("""
        SELECT match_id, home_team, away_team FROM kessbet_match
        WHERE platform='kessbet' AND sport='sr:sport:1'
          AND kickoff_time > NOW()
        ORDER BY kickoff_time LIMIT %s
        """, (limit,))
        metas = [{'match_id': r[0], 'home_team': r[1], 'away_team': r[2]}
                 for r in c.fetchall()]

    print('扫描 %d 场未开赛比赛（同一套解析器）' % len(metas), flush=True)
    n_suspect = 0
    n_checked = 0
    for meta in metas:
        try:
            info = dump_match(adapter, dao, meta)
        except Exception as e:
            print('  %s 失败: %s' % (meta.get('home_team'), e))
            continue
        if not info:
            continue
        n_checked += 1
        if info['suspects']:
            n_suspect += len(info['suspects'])
            show(info)
    print('=' * 78)
    print('共检查 %d 场，发现内部和异常盘口 %d 个' % (n_checked, n_suspect))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--loop', type=int, default=0,
                    help='每隔 N 秒重复，0=只跑一次')
    ap.add_argument('--upcoming', type=int, default=0, metavar='N',
                    help='改扫 N 场未开赛比赛（验证解析器本身）')
    args = ap.parse_args()

    adapter = KessbetAdapter()
    dao = Dao()
    try:
        if args.upcoming:
            scan_upcoming(adapter, dao, args.upcoming)
            return

        while True:
            metas = dao.live_analyze_targets(150)
            if not metas:
                print('当前没有进行中的滚球比赛', flush=True)
            for meta in metas:
                try:
                    info = dump_match(adapter, dao, meta)
                except Exception as e:
                    print('  %s 失败: %s' % (meta.get('home_team'), e))
                    continue
                if info:
                    show(info)
            if not args.loop:
                break
            time.sleep(args.loop)
    finally:
        dao.close()


if __name__ == '__main__':
    main()
