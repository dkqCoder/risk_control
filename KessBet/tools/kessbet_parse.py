"""把 KessBet 的单场盘口 JSON 解析成扁平 CSV。

用法：
    py kessbet_parse.py --prepare   # 从 kessbet_soccer_raw.json 生成 match_ids.json
    py kessbet_parse.py             # 解析 raw_odds/ 生成 kessbet_all_odds.csv
"""
import json, csv, glob, os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, 'data')
SCHEDULE = os.path.join(DATA, 'kessbet_soccer_raw.json')
OUT_DIR = os.path.join(DATA, 'odds', 'raw_odds')
ID_FILE = os.path.join(DATA, 'match_ids.json')
CSV_OUT = os.path.join(DATA, 'kessbet_all_odds.csv')


def num(x):
    try:
        v = float(x)
        return v if 1.0 <= v <= 10000 else None
    except Exception:
        return None


def split_segments(sec):
    """Split on '^' and on '@' (multi-leg selections)."""
    out = []
    for chunk in sec.split('^'):
        for part in chunk.split('@'):
            out.append(part)
    return out


def parse_mari(mari):
    """Return (market_name, groups, [(line, label, odds)])."""
    if not mari or '|' not in mari:
        return None, '', []
    market = mari.rsplit('|', 1)[-1]
    body = mari.rsplit('|', 1)[0]

    parts = body.split('|')
    groups = ''
    sec = None
    for i, p in enumerate(parts):
        if '^' in p or '@' in p:
            sec = p
            groups = parts[i - 1] if i > 0 else ''
            break
    if sec is None:
        # single-selection market: last segment, strip leading "matchId,1,"
        sec = parts[-1]
        groups = parts[-2] if len(parts) > 1 else ''

    sels = []
    for seg in split_segments(sec):
        f = seg.split(',')
        if len(f) < 4:
            continue
        odds = num(f[-3])
        if odds is None:
            continue
        label = f[-1].strip()
        # Line = everything before the id/odds triple
        line_raw = ','.join(f[:-4]) if len(f) > 4 else ''
        # split "label,idpair,odds,1,LABEL" -> line may hold "2.5" or "2.5,<ids>/1=<ids>"
        if line_raw:
            line = line_raw.split(',')[0].strip()
        else:
            line = ''
        sels.append((line, label, f[-3]))

    # second pass for multi-leg (over@under) where each leg has leading odds-less prefix
    if not sels:
        for seg in split_segments(sec):
            f = seg.split(',')
            for j in range(len(f) - 1):
                if num(f[j]) and j + 2 < len(f) and f[j + 2] == '1':
                    sels.append((f[0] if j > 0 else '', f[-1], f[j]))

    return market, groups, sels


def normalize_market(name, home, away):
    """Strip team-name prefixes so markets group together; report which side they describe."""
    if home and name.startswith(home + ' '):
        return '主队 ' + name[len(home) + 1:], home
    if away and name.startswith(away + ' '):
        return '客队 ' + name[len(away) + 1:], away
    return name, ''


def fill_forward(sels):
    """Lines are only emitted on the first leg of each group; carry them forward."""
    out, cur = [], ''
    for line, label, odds in sels:
        if line:
            cur = line
        out.append((cur, label, odds))
    return out


def load_schedule():
    raw = json.load(open(SCHEDULE, encoding='utf-8'))
    matches = {}
    for g in raw['data']['leagueGroupList']:
        for m in g['matchList']:
            matches[m['matchId']] = m
    return matches


def prepare_ids():
    matches = load_schedule()
    ids = list(matches)
    json.dump(ids, open(ID_FILE, 'w'), indent=0)
    print(f'wrote {len(ids)} match ids -> {ID_FILE}')


def main():
    matches = load_schedule()
    names = {mid: (m.get('htn', ''), m.get('atn', '')) for mid, m in matches.items()}

    rows = []
    unmatched = []
    for path in glob.glob(os.path.join(OUT_DIR, '*.json')):
        mid = os.path.splitext(os.path.basename(path))[0]
        info = matches.get(mid)
        if not info:
            unmatched.append(mid)
            continue
        markets = json.load(open(path, encoding='utf-8'))
        home, away = names.get(mid, ('', ''))
        for mk in markets:
            name, groups, sels = parse_mari(mk.get('mari', ''))
            if name is None:
                continue
            name, side = normalize_market(name, home, away)
            for line, label, odds in fill_forward(sels):
                rows.append({
                    '日期': info.get('mdt', ''),
                    '开赛': info.get('mbt', ''),
                    '联赛': info.get('ln', ''),
                    '主队': home,
                    '客队': away,
                    '盘口': name,
                    '归属': side,
                    '盘线': line,
                    '选项': label,
                    '赔率': odds,
                    'matchId': mid,
                })

    with open(CSV_OUT, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print('rows', len(rows), '->', CSV_OUT)
    print('matches with odds', len({r['matchId'] for r in rows}), '/', len(matches))
    print('unmatched ids', len(unmatched))

    by_market = {}
    for r in rows:
        by_market.setdefault(r['盘口'], 0)
        by_market[r['盘口']] += 1
    print('distinct markets', len(by_market))
    for k, v in sorted(by_market.items(), key=lambda x: -x[1])[:25]:
        print(f'  {v:>7}  {k}')


if __name__ == '__main__':
    if '--prepare' in sys.argv:
        prepare_ids()
    else:
        main()
