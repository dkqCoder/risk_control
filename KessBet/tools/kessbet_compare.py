"""生成官网页面 vs 接口数据的对照 HTML。"""
import json, sys, os, html, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import kessbet_arb as P

DATA = os.path.join(BASE, 'data', 'snapshots')
OFFICIAL = os.path.join(DATA, '_official_page.json')
API = os.path.join(DATA, '_api_snapshot.json')
TOOLS = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(TOOLS, 'odds_compare_template.html')
OUT = os.path.join(TOOLS, 'odds_compare.html')

HOME, AWAY = 'Arsenal', 'Everton'
MID = '245131096595927104'


def parse_official():
    d = json.load(open(OFFICIAL, encoding='utf-8'))
    out = []
    for box in d['data']:
        title = box['title']
        rows = []
        header = None
        for r in box['rows']:
            cells = [c['txt'].strip() for c in r['cells']]
            if r['isLabel']:
                header = [c for c in cells if c]
                continue
            if not cells:
                continue
            if len(cells) == 1:
                # "1 1.47" 形式
                parts = cells[0].split()
                if len(parts) >= 2:
                    rows.append({'line': '', 'label': parts[0], 'odds': parts[-1]})
                continue
            if header:
                # 有表头：首列是盘线(可能为空)，其余按表头对齐
                line = cells[0]
                vals = cells[1:]
                hdr_vals = header[1:] if header and header[0] == '' else header
                for i, v in enumerate(vals):
                    lab = hdr_vals[i] if i < len(hdr_vals) else ''
                    rows.append({'line': line, 'label': lab, 'odds': v})
            else:
                for c in cells:
                    parts = c.split()
                    if len(parts) >= 2:
                        rows.append({'line': '', 'label': parts[0], 'odds': parts[-1]})
        if rows:
            out.append({'title': title, 'rows': rows})
    return out


def parse_api():
    snap = json.load(open(API, encoding='utf-8'))
    markets = {}
    for mk in snap['data']:
        nm, sels = P.parse_mari(mk['mari'])
        if not nm:
            continue
        markets[nm] = sels
    return markets, snap['ts']


def norm_key(title):
    """把官网标题（

含队名）映射到接口盘口名。"""
    return title


def main():
    official = parse_official()
    api, ts = parse_api()

    # 建立对照表
    blocks = []
    matched = 0
    mismatched = 0
    for box in official:
        title = box['title']
        base = title
        side = ''
        if title.startswith(HOME + ' '):
            side, base = 'H', title[len(HOME) + 1:]
        elif title.startswith(AWAY + ' '):
            side, base = 'A', title[len(AWAY) + 1:]

        api_sels = api.get(title)
        api_map = {}
        if api_sels:
            for line, lab, odds in api_sels:
                api_map[(line, lab)] = odds

        rows = []
        for r in box['rows']:
            key = (r['line'], r['label'])
            api_odds = api_map.get(key)
            if api_odds is None and r['line'] == '':
                # 尝试不带盘线匹配
                for (al, alab), ao in api_map.items():
                    if alab == r['label'] and al == '':
                        api_odds = ao
                        break
            try:
                same = api_odds is not None and abs(float(r['odds']) - float(api_odds)) < 1e-9
            except ValueError:
                same = False
            if api_odds is not None:
                matched += 1
                if not same:
                    mismatched += 1
            rows.append({'line': r['line'], 'label': r['label'],
                         'web': r['odds'], 'api': api_odds if api_odds else '',
                         'same': same})
        blocks.append({'title': title, 'rows': rows, 'has_api': api_sels is not None})

    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    rows_html = []
    for b in blocks:
        rows_html.append(f'<div class="market"><h3>{html.escape(b["title"])}'
                         f'{" <span class=miss>接口中无此盘口</span>" if not b["has_api"] else ""}</h3>')
        rows_html.append('<table><tr><th>盘线</th><th>选项</th><th>官网页面</th><th>接口抓取</th><th>比对</th></tr>')
        for r in b['rows']:
            mark = '✓' if r['same'] else ('✗' if r['api'] else '—')
            cls = 'ok' if r['same'] else ('bad' if r['api'] else 'na')
            rows_html.append(
                f'<tr class="{cls}"><td>{html.escape(r["line"])}</td>'
                f'<td>{html.escape(r["label"])}</td>'
                f'<td>{html.escape(r["web"])}</td>'
                f'<td>{html.escape(str(r["api"]))}</td>'
                f'<td class="mark">{mark}</td></tr>')
        rows_html.append('</table></div>')

    tpl = open(TEMPLATE, encoding='utf-8').read()
    out = (tpl.replace('{{TITLE}}', f'{HOME} vs {AWAY}')
              .replace('{{MID}}', MID)
              .replace('{{NOW}}', now)
              .replace('{{APITS}}', ts)
              .replace('{{MATCHED}}', str(matched))
              .replace('{{MISMATCHED}}', str(mismatched))
              .replace('{{BODY}}', '\n'.join(rows_html)))
    open(OUT, 'w', encoding='utf-8').write(out)
    print(f'生成 {OUT}')
    print(f'比对: 匹配 {matched} 项, 不一致 {mismatched} 项')
    print(f'盘口: 官网 {len(official)} 个, 接口 {len(api)} 个')


if __name__ == '__main__':
    main()
