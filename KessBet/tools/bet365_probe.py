"""tools/bet365_probe.py — bet365 抓取可用性自检。

    py tools/bet365_probe.py                     # 探活 + 打印一个联赛的赔率
    py tools/bet365_probe.py --league 135650998  # 指定联赛
    py tools/bet365_probe.py --json              # 输出 JSON
    py tools/bet365_probe.py --no-browser        # 只测 HTTP（预期失败，用于诊断）

退出码：0 = 拿到数据；1 = 失败。

## 为什么默认走浏览器

bet365 的体育内容接口（`/contentdata/...`）对外部调用一律返回 200 + 空体，
必须在真实浏览器页面里用站点自己的 `Loader` 类发请求才行。
详见 `collect/bet365/browser.py` 文件头。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from collect.bet365 import league
from collect.bet365.browser import Bet365Browser

PREMIER_LEAGUE = '91422157'
CHAMPIONSHIP = '135650998'


def http_probe(proxy: str) -> dict:
    """纯 HTTP 探活。预期：首页有数据，体育内容为空（这是被拒的证据）。"""
    from collect.bet365.http import Bet365Http
    h = Bet365Http(proxy=proxy)
    out = {}
    try:
        h.session
        out['home_page'] = 'ok'
    except Exception as e:
        return {'home_page': f'FAIL {e}'}
    out['home_pod'] = len(h._get('/homepagecontentapi/pod', '#HO#P31524#'))
    for name, path in [('soccer_tab', '/splashcontentapi/soccertab'),
                       ('contentdata', '/contentdata/matchmarketscontentapi/markets')]:
        try:
            out[name] = len(h._get(path, '#AS#B1#K^5#'))
        except Exception as e:
            out[name] = f'ERR {e}'
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description='bet365 抓取可用性自检')
    ap.add_argument('--proxy', default='http://127.0.0.1:7897')
    ap.add_argument('--league', default=PREMIER_LEAGUE, help='联赛 ID，默认英超')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--no-browser', action='store_true',
                    help='只测纯 HTTP（预期拿不到内容，用于诊断）')
    ap.add_argument('--show-headless', action='store_true', help='Chrome 用无头模式')
    args = ap.parse_args()

    print('=== 纯 HTTP 探活 ===')
    hp = http_probe(args.proxy)
    for k, v in hp.items():
        print(f'  {k:12} {v}')
    if args.no_browser:
        return 0 if hp.get('home_pod') else 1

    print(f'\n=== 浏览器取数（联赛 {args.league}）===')
    try:
        with Bet365Browser(proxy=args.proxy, headless=args.show_headless) as b:
            catalog = b.fetch_markets(args.league)
            s = league.summarize(catalog)
            print(f'  盘口目录: {s["bytes"]} 字节 / {s["fixtures"]} 场 / {s["markets"]} 个盘口')
            if s['bytes'] == 0:
                print('  ✗ 目录为空 —— 检查代理出口（需能正常展示体育内容的节点）',
                      file=sys.stderr)
                return 1

            markets = league.parse_market_catalog(catalog)
            x12 = next((m for m in markets if m['market'] in ('全场赛果', 'Full Time Result')), None)
            if not x12:
                print('  ✗ 目录里没有 1X2 盘口', file=sys.stderr)
                return 1

            detail = b.fetch_market_detail(x12['pd'])
            table = league.build_market_table(detail)
            fixtures = {f['name']: f for f in league.parse_fixtures(detail)}
    except Exception as e:
        print(f'  ✗ {type(e).__name__}: {e}', file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({
            'catalog': s,
            'markets': [m['market'] for m in markets],
            'x12': table,
            'fixtures': list(fixtures.values()),
        }, ensure_ascii=False, indent=2, default=str))
        return 0

    print(f'\n  {x12["market"]}：{len(table)} 场')
    costs = []
    for name, sels in table.items():
        fx = fixtures.get(name, {})
        parts = '  '.join(f'{k}={v}' for k, v in sels.items() if k in ('1', 'X', '2'))
        cost = sum(1 / v for v in sels.values()) if all(k in sels for k in ('1', 'X', '2')) else None
        if cost:
            costs.append(cost)
        extra = f'  覆盖={cost:.4f}' if cost else ''
        print(f'    {name:24} {parts}{extra}')
        if fx.get('sportradar_id'):
            print(f'    {"":24} {fx["sportradar_id"]}  {fx.get("kickoff_uk")} (UK)')

    if costs:
        print(f'\n  覆盖成本: min={min(costs):.4f}  avg={sum(costs)/len(costs):.4f}  max={max(costs):.4f}')
        print('  （1.0 = 无抽水，>1 = 博彩公司抽水。正常在 1.03~1.10）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
