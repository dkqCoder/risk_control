"""盯住滚球比赛，一旦出现就抓原始串做对比。

## 要回答的问题

2026-11-13 发现一条假套利，选中的盘口三个选项加起来只有 0.108：

    1st Half - Handicap 0:2   1@75.0   2@85.0   X@12.0

三选一盘口加总 0.108 在数学上不可能。两种可能：
  A. 平台真的返回了这种报价
  B. 我们解析错了（把不同盘线的选项混到同一个 line 下）

预赛数据扫了 25 场，解析器没有出现任何重复或错位，所以这个现象
只在滚球时出现 —— 需要抓一份滚球时刻的原始串才能定论。

## 这个脚本做什么

每 30 秒查一次有没有进行中的比赛。一旦发现，就对每场比赛：
  1. 拉原始 `mari` 串
  2. 按我们的解析器解析
  3. 逐个盘口比较「原始串里的选项」vs「解析结果」
  4. 把不一致的、内部和异常的、有重复标签的，全部写进日志

抓到数据后继续观察若干轮（默认 6 轮，约 3 分钟），然后退出。

用法：
    py tools/watch_live_raw.py                  # 等比赛，抓到就退出
    py tools/watch_live_raw.py --max-minutes 90 # 最多等 90 分钟
"""
import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collect.kessbet.adapter import KessbetAdapter
from collect.kessbet.parse import parse_mari, split_team
from db.dao import Dao

LOG = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'logs', 'live_raw_probe.log')

SUSPECT_SUM = 0.95
POLL_SEC = 30
SNAPSHOTS_PER_MATCH = 999      # 一直抓到比赛结束
SNAPSHOT_INTERVAL_SEC = 120    # 同一场两次抓取的最小间隔


def log(msg=''):
    line = str(msg)
    print(line, flush=True)
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def analyse(adapter, meta):
    """抓一场，返回可疑项列表。"""
    mid = meta['match_id']
    home = meta.get('home_team') or ''
    away = meta.get('away_team') or ''

    raw = adapter.fetch_raw(mid)
    if not raw:
        return None

    parsed = defaultdict(dict)          # (market, side, line) -> {label: odds}
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
                parsed[key][label + '#dup'] = odds
            parsed[key][label] = odds

    suspects = []
    for key, sels in parsed.items():
        total = sum(1.0 / o for o in sels.values() if o > 0)
        dup = any('#dup' in k for k in sels)
        if total < SUSPECT_SUM or dup:
            suspects.append((key, sels, total, dup,
                             raw_by_market.get(key[:2], [])))
    return {'mid': mid, 'home': home, 'away': away,
            'n_raw': len(raw), 'n_markets': len(parsed),
            'suspects': suspects}


def report(info):
    log('=' * 78)
    log('[%s] %s vs %s  (matchId %s)' % (
        datetime.now().strftime('%H:%M:%S'),
        info['home'][:26], info['away'][:26], info['mid']))
    log('平台返回 %d 个盘口，解析出 %d 个 (market, side, line) 组'
        % (info['n_raw'], info['n_markets']))

    if not info['suspects']:
        log('  ✓ 无异常')
        return

    for (market, side, line), sels, total, dup, raws in info['suspects']:
        log()
        log('  ⚠ %s | %s | %s   Σ=%.4f%s'
            % (market, side or '-', line or '-', total,
               '   ★ 同盘线重复选项' if dup else ''))
        log('     解析结果:')
        for lab, o in sorted(sels.items()):
            log('       %-10s @%-10s  1/赔率=%.4f' % (lab, o, 1.0 / o))
        log('     原始串:')
        for r in raws:
            log('       %s' % r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-minutes', type=int, default=60,
                    help='最多等待多少分钟')
    args = ap.parse_args()

    adapter = KessbetAdapter()
    dao = Dao()
    deadline = time.monotonic() + args.max_minutes * 60
    done = defaultdict(int)
    last_seen = {}
    found_any = False

    log()
    log('### 开始监听滚球原始赔率  %s' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    try:
        while time.monotonic() < deadline:
            metas = dao.live_analyze_targets(150)
            if not metas:
                time.sleep(POLL_SEC)
                continue

            found_any = True
            for meta in metas:
                mid = meta['match_id']
                if done[mid] >= SNAPSHOTS_PER_MATCH:
                    continue
                # 同一场两次抓取之间至少隔 2 分钟 —— 盘口形态要过几分钟
                # 才会变化，抓太密没信息量。
                if time.monotonic() - last_seen.get(mid, 0) < SNAPSHOT_INTERVAL_SEC:
                    continue
                last_seen[mid] = time.monotonic()
                try:
                    info = analyse(adapter, meta)
                except Exception as e:
                    log('  %s 抓取失败: %s' % (meta.get('home_team'), e))
                    continue
                if info:
                    report(info)
                    done[mid] += 1

            time.sleep(POLL_SEC)

        if not found_any:
            log('### 等待期内没有出现滚球比赛')
        else:
            log('### 结束  %s' % datetime.now().strftime('%H:%M:%S'))
    finally:
        dao.close()


if __name__ == '__main__':
    main()
