"""量化「残缺盘口」对套利判定的影响。

## 背景

监听 231 轮滚球数据发现：127 个「内部和异常」的盘口全部是同一个形态 ——
平台锁掉一条腿，只剩另一条可投注：

    Both Teams To Score
      yes  0.00,0   ← 锁定（标记 ,0,）
      no   4.90     ← 唯一可投注

这类盘口自己无法构成覆盖，但剩下那条腿可以和其他盘口组合。
而平台锁腿通常意味着「这个盘口马上要结算」，剩那条腿的价格是冻结的 ——
拿它拼出「套利」，实际下不了注。

## 这个脚本做什么

对当前所有滚球比赛：
  1. 统计有多少盘口是残缺的
  2. 去掉残缺盘口后，有多少场比赛从「能覆盖」变成「无法覆盖」
  3. 对比去掉前后的成本

用法：
    py tools/check_incomplete_markets.py
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyze.live import result as R
from db.dao import Dao

# 各类盘口「完整」时应该有哪些标签
EXPECTED = {
    '1x2': {'1', 'X', '2'},
    'Double Chance': {'1X', '12', 'X2'},
    'Both Teams To Score': {'yes', 'no'},
    'Handicap': {'1', 'X', '2'},
    '1st Half - 1x2': {'1', 'X', '2'},
    '1st Half - Double Chance': {'1X', '12', 'X2'},
    '1st Half - Both Teams To Score': {'yes', 'no'},
    '1st Half - Handicap': {'1', 'X', '2'},
    '2nd Half - 1x2': {'1', 'X', '2'},
    '2nd Half - Double Chance': {'1X', '12', 'X2'},
    '2nd Half - Both Teams To Score': {'yes', 'no'},
}


def expected_for(market, line):
    """这条盘线「完整」时应该有哪些标签。未知返回 None。"""
    if market in EXPECTED:
        return EXPECTED[market]
    if 'Total' in market and not market.endswith('Goals'):
        # 所有 over/under 类盘口，按盘线区分
        return {'over', 'under'}
    return None


def split_complete(selections):
    """拆成（完整盘口的选项，残缺盘口的选项）。"""
    groups = defaultdict(list)
    for mkt, side, line, lab, odds in selections:
        if not R.is_result_market(mkt):
            continue
        groups[(mkt, side, line)].append((lab, odds))

    good, bad = [], []
    for (mkt, side, line), sels in groups.items():
        labels = {lab for lab, _ in sels}
        exp = expected_for(mkt, line)
        if exp is None:
            good.extend((mkt, side, line, lab, o) for lab, o in sels)
        elif exp.issubset(labels):
            good.extend((mkt, side, line, lab, o) for lab, o in sels)
        else:
            bad.extend((mkt, side, line, lab, o) for lab, o in sels)
    return good, bad


def main():
    dao = Dao()
    try:
        targets = dao.live_analyze_targets(150)
        print('进行中比赛 %d 场\n' % len(targets))

        for t in targets:
            mid = t['match_id']
            sels = dao.get_live_selections(mid)
            good, bad = split_complete(sels)
            if not bad:
                continue

            bad_mkts = {(m, s, l) for m, s, l, _, _ in bad}
            print('=' * 74)
            print('%s vs %s  第%s分钟 %s:%s'
                  % (t['home_team'][:22], t['away_team'][:22],
                     t['minute'], t['home_score'], t['away_score']))
            print('  残缺盘口 %d 个（%d 个选项）'
                  % (len(bad_mkts), len(bad)))
            for m, s, l in sorted(bad_mkts)[:6]:
                labs = [x[3] for x in bad if (x[0], x[1], x[2]) == (m, s, l)]
                exp = expected_for(m, l)
                miss = (exp - set(labs)) if exp else {'?'}
                print('    %-34s %-2s %-5s 有 %-14s 缺 %s'
                      % (m[:34], s or '-', l or '-',
                         ','.join(sorted(labs)), ','.join(sorted(miss))))

            # 去掉残缺盘口后还能不能覆盖
            r_all = R.check_match(sels, t['home_score'], t['away_score'],
                                  minute=t['minute'])
            r_good = R.check_match(good, t['home_score'], t['away_score'],
                                   minute=t['minute'])
            print('  含残缺: %s' % ('无覆盖' if r_all is None
                                    else 'Σ=%.4f' % r_all['cost']))
            print('  只完整: %s' % ('无覆盖' if r_good is None
                                    else 'Σ=%.4f' % r_good['cost']))
            print()
    finally:
        dao.close()


if __name__ == '__main__':
    main()
