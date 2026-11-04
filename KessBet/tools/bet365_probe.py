"""tools/bet365_probe.py — bet365 抓取可用性自检。

    py tools/bet365_probe.py            # 探活 + 打印拿到的事件和盘口
    py tools/bet365_probe.py --json     # 输出 JSON

它回答三个问题：
  1. 当前代理出口能不能访问 bet365？
  2. 哪些内容接口有数据（字节数 > 0）？
  3. 首页接口拿到的比赛和盘口长什么样？

退出码：0 = 至少首页接口有数据；1 = 全部为空（出口被降级）。
"""
import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK，队名里的特殊字符会炸；统一转 UTF-8
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from collect.bet365 import parse
from collect.bet365.http import Bet365Http, EmptyContentError


def main() -> int:
    ap = argparse.ArgumentParser(description='bet365 抓取可用性自检')
    ap.add_argument('--proxy', default=None, help='代理地址，默认读 BET365_PROXY')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(message)s')
    http = Bet365Http(proxy=args.proxy)

    try:
        http.session  # 触发首页握手，拿 cookie
    except Exception as e:
        print(f'✗ 无法访问 bet365 首页: {e}', file=sys.stderr)
        print('  检查代理是否在跑（BET365_PROXY / 默认 127.0.0.1:7897）', file=sys.stderr)
        return 1

    probe = http.probe()
    if args.json:
        print(json.dumps({'probe': probe}, ensure_ascii=False, indent=2))
    else:
        print('接口探活：')
        for name, info in probe.items():
            if 'error' in info:
                print(f'  ✗ {name:14} {info["error"]}')
            else:
                mark = '✓' if info['bytes'] > 0 else '✗'
                print(f'  {mark} {name:14} {info["bytes"]:>8} 字节  {info["secs"]}s')

    home = probe.get('home_pod', {})
    if home.get('bytes', 0) <= 0:
        print('\n首页接口也没有数据 —— 当前出口 IP 被 bet365 降级，换代理节点。',
              file=sys.stderr)
        return 1

    text = http.fetch_pod()
    events = parse.parse_events(text)
    odds = parse.parse_odds(text)
    summary = parse.summarize(text)

    if args.json:
        print(json.dumps({
            'summary': summary,
            'events': events,
            'odds': odds,
        }, ensure_ascii=False, indent=2))
        return 0

    print(f'\n首页快照：{summary["events"]} 场 / {summary["selections"]} 个选项 / '
          f'{summary["markets"]} 种盘口 / {summary["bytes"]} 字节')
    for ev in events:
        rows = odds.get(ev['name'], [])
        print(f'\n  {ev["name"]}  [{ev["competition"]}]  {len(rows)} 个选项')
        for r in rows:
            line = f' {r["line"]}' if r['line'] else ''
            print(f'      {r["market"]:12} {r["selection"]}{line:8}  '
                  f'{r["odds_raw"]:>8} → {r["odds"]}')

    if probe.get('soccer_tab', {}).get('bytes', 0) <= 0:
        print('\n注：足球页/单场全盘口接口仍为空（出口 IP 被降级），'
              '当前只能拿到首页精选。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
