"""用**平台实时全量接口**验证比赛结果套利判定。

## 为什么不用历史流水回测

流水表只记录「变化」和「新增」，**不记录「撤盘」**。而平台会主动撤盘：
  - Double Chance：1X 撤了换成 12+X2
  - Total：2.5 替换了 1.5
  - Correct Score：比分逐批替换

任何从流水重建快照的做法，都会让已撤的选项留下来，和当前选项
拼出真实环境不可能出现的组合。实测踩了三次，每次都算出 Σ=0.05~0.9
的荒谬结果 —— 那些都不是真套利，是重建错误。

「只用同一批次」也不行：一批只有 3~20 个选项，永远构成不了完整覆盖。

## 唯一可靠的验证

`fetch_odds(match_id)` 返回的是**平台当前完整盘口快照** ——
和用户在网页上看到的完全一致。拿它跑判定，结果是可信的。

## 用法

    py tools/live_result_check.py            # 扫当前所有进行中的足球
    py tools/live_result_check.py --minutes  # 监测一段时间，看赔率变化
"""
import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyze.live import result as R
from collect.kessbet.adapter import KessbetAdapter
from db.dao import Dao

log = logging.getLogger('live_result_check')


def check_one(adapter, dao, meta):
    """对一场跑实时判定。"""
    mid = meta['match_id']
    state = adapter.fetch_state(mid)
    if not state:
        return None, 'no_state'

    hs = state.get('hmp')
    as_ = state.get('amp')
    if hs in (None, '') or as_ in (None, ''):
        return None, 'no_score'
    hs, as_ = int(hs), int(as_)

    mt = state.get('mt') or ''
    minute = None
    if ':' in str(mt):
        try:
            minute = int(str(mt).split(':')[0])
        except ValueError:
            pass

    # 半场比分：从流水反推
    ht = None
    if minute is not None and minute > 45:
        rows = dao.live_history_for_half_time(mid)
        ht = R.half_time_score(rows)

    odds = adapter.fetch_odds(mid, meta.get('home_team') or '',
                              meta.get('away_team') or '')
    if not odds:
        return None, 'no_odds'

    sels = [(s.market, s.side, s.line, s.label, float(s.odds)) for s in odds]
    r = R.check_match(sels, hs, as_, minute=minute,
                      ht_home=ht[0] if ht else None,
                      ht_away=ht[1] if ht else None)
    if r is None:
        return None, 'no_cover'

    r['home'] = hs
    r['away'] = as_
    r['minute'] = minute
    r['ht'] = ht
    r['n_total_selections'] = len(sels)
    return r, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--loop', type=int, default=0,
                    help='每隔 N 秒重复一次，0=只跑一次')
    ap.add_argument('--rounds', type=int, default=0,
                    help='重复多少轮，0=无限')
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING,
                        format='%(asctime)s [%(levelname)s] %(message)s',
                        datefmt='%H:%M:%S')

    adapter = KessbetAdapter()
    dao = Dao()
    n_round = 0

    try:
        while True:
            n_round += 1
            meta_list = dao.live_analyze_targets(150)
            print('=' * 78)
            print('第 %d 轮  %d 场进行中'
                  % (n_round, len(meta_list)), flush=True)
            print('=' * 78)

            for meta in meta_list:
                mid = meta['match_id']
                try:
                    r, err = check_one(adapter, dao, meta)
                except Exception as e:
                    print('  %-20s ✗ %s' % (meta['home_team'][:20], e),
                          flush=True)
                    continue

                name = '%s vs %s' % (meta['home_team'][:18],
                                     meta['away_team'][:18])
                if r is None:
                    print('  %-40s —  %s' % (name, err), flush=True)
                    continue

                flag = '★ 套利' if r['is_arbitrage'] else '        '
                print('  %-40s %s  Σ=%.4f  第%s分钟 %s:%s  可达%-5d 候选%-4d'
                      % (name, flag, r['cost'], r['minute'], r['home'],
                         r['away'], r['reach'], r['n_candidates']), flush=True)
                if r['is_arbitrage']:
                    for d, o, w in R.stakes(r['chosen'], r['cost']):
                        print('        %-40s @ %.2f  %.1f%%'
                              % (d, o, w * 100), flush=True)
            print(flush=True)

            if not args.loop:
                break
            if args.rounds and n_round >= args.rounds:
                break
            time.sleep(args.loop)
    finally:
        dao.close()


if __name__ == '__main__':
    main()
