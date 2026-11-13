"""A/B 对比：旧解析逻辑 vs 新解析逻辑，用真实原始串验证。

## 背景

怀疑 parse_mari 有「盘线串门」bug：某条盘线的**第一个选项**被锁定
或赔率非法时，盘线名丢失，后续选项被算到上一条盘线头上。

已修复（先取盘线前缀，再解析选项）。但需要确认：
  1. 这个 bug 在真实数据里**到底会不会触发**
  2. 触发时输出差异有多大

## 方法

同一批原始 mari 串，分别用两种逻辑解析，逐条比较：
  旧：`if line: current_line = line`（只有解析成功才更新盘线）
  新：`_line_prefix(seg)` 先取盘线，与选项是否有效无关

任何差异都打印出来，附原始串。

用法：
    py tools/check_parse_ab.py                # 扫未开赛比赛
    py tools/check_parse_ab.py --live         # 扫进行中的比赛
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collect.kessbet.adapter import KessbetAdapter
from collect.kessbet.parse import _line_prefix, _split_selection
from db.dao import Dao


def parse_old(mari: str):
    """修复前的逻辑（原样复刻）。"""
    if not mari or '|' not in mari:
        return None, []
    name = mari.rsplit('|', 1)[-1]
    body = mari.rsplit('|', 1)[0]

    section = None
    for part in body.split('|'):
        if '^' in part or '@' in part:
            section = part
            break
    if section is None:
        section = body.split('|')[-1]

    sels = []
    current_line = ''
    for chunk in section.split('^'):
        for seg in chunk.split('@'):
            parsed = _split_selection(seg)
            if parsed is None:
                continue
            line, label, odds = parsed
            if line:
                current_line = line
            sels.append((current_line, label, odds))
    return name, sels


def parse_new(mari: str):
    """修复后的逻辑。"""
    if not mari or '|' not in mari:
        return None, []
    name = mari.rsplit('|', 1)[-1]
    body = mari.rsplit('|', 1)[0]

    section = None
    for part in body.split('|'):
        if '^' in part or '@' in part:
            section = part
            break
    if section is None:
        section = body.split('|')[-1]

    sels = []
    current_line = ''
    for chunk in section.split('^'):
        for seg in chunk.split('@'):
            prefix = _line_prefix(seg)
            if prefix:
                current_line = prefix
            parsed = _split_selection(seg)
            if parsed is None:
                continue
            _, label, odds = parsed
            sels.append((current_line, label, odds))
    return name, sels


def market_sums(sels):
    g = defaultdict(list)
    for line, label, odds in sels:
        g[line].append(odds)
    return {k: sum(1.0 / o for o in v) for k, v in g.items()}


def compare(mari, home, away):
    """返回差异描述，无差异返回 None。"""
    _, old = parse_old(mari)
    _, new = parse_new(mari)
    if old == new:
        return None

    name = mari.rsplit('|', 1)[-1]
    old_sum = market_sums(old)
    new_sum = market_sums(new)
    return {
        'name': name, 'old': old, 'new': new,
        'old_sum': old_sum, 'new_sum': new_sum, 'mari': mari,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true', help='扫进行中的比赛')
    ap.add_argument('--limit', type=int, default=40)
    args = ap.parse_args()

    adapter = KessbetAdapter()
    dao = Dao()
    try:
        with dao.conn.cursor() as c:
            if args.live:
                c.execute("""
                SELECT match_id, home_team, away_team FROM kessbet_match
                WHERE platform='kessbet' AND sport='sr:sport:1'
                  AND kickoff_time <= NOW()
                  AND kickoff_time > NOW() - INTERVAL 150 MINUTE
                """)
            else:
                c.execute("""
                SELECT match_id, home_team, away_team FROM kessbet_match
                WHERE platform='kessbet' AND sport='sr:sport:1'
                  AND kickoff_time > NOW()
                ORDER BY kickoff_time LIMIT %s
                """, (args.limit,))
            metas = c.fetchall()

        label = '进行中' if args.live else '未开赛'
        print('A/B 对比 %d 场%s比赛' % (len(metas), label))

        n_raw = 0
        diffs = []
        for mid, home, away in metas:
            try:
                raw = adapter.fetch_raw(mid)
            except Exception as e:
                print('  %s 拉取失败: %s' % (home[:16], e))
                continue
            n_raw += len(raw)
            for mk in raw:
                mari = mk.get('mari') or ''
                d = compare(mari, home, away)
                if d:
                    diffs.append((home, away, d))

        print('平台盘口总数 %d，解析结果有差异的 %d 个' % (n_raw, len(diffs)))
        print()

        for home, away, d in diffs[:15]:
            print('=' * 78)
            print('%s vs %s   盘口 %s' % (home[:20], away[:20], d['name']))
            print('  旧: %s' % d['old'])
            print('  新: %s' % d['new'])
            print('  旧内部和: %s' % {k: round(v, 3) for k, v in d['old_sum'].items()})
            print('  新内部和: %s' % {k: round(v, 3) for k, v in d['new_sum'].items()})
            print('  原始串: %s' % d['mari'][:320])

        if not diffs:
            print('✓ 两种逻辑输出完全一致 —— 这个 bug 在当前数据里没有触发')
    finally:
        dao.close()


if __name__ == '__main__':
    main()
