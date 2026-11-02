"""KessBet 数据抓取客户端（纯 Python，无需浏览器）。

用法:
    py kessbet_client.py list                 # 拉全部赛程
    py kessbet_client.py list --today         # 只看今天
    py kessbet_client.py odds <matchId>       # 拉单场全盘口
    py kessbet_client.py odds <matchId> -o f.json

依赖: pip install curl_cffi pycryptodome
必须能访问 api.kessbet.com（中国大陆 IP 会被 Cloudflare 403，需走境外代理）。
"""
import argparse
import base64
import datetime
import json
import os
import sys
import urllib.parse
from collections import Counter

from Crypto.Cipher import AES
from curl_cffi import requests as creq

BASE = os.path.dirname(os.path.abspath(__file__))
API = 'https://api.kessbet.com/cpop/ots/unct/public/handler'
KEY = b'QipVAlnxoMjeQ4I47ne7kWDk'   # AES-192，前端 JS 里硬编码
IV = b'0000000000000000'
DEFAULT_PROXY = 'http://127.0.0.1:7897'

HDRS = {
    'content-type': 'application/x-www-form-urlencoded;charset=UTF-8',
    'origin': 'https://www.kessbet.com',
    'referer': 'https://www.kessbet.com/',
}

# 实测有效的 action
ACT_LIST = '00252'    # 赛程列表（游标分页）
ACT_COUNT = '00251'   # 各维度场次统计
ACT_MATCH = '0015'    # 单场全盘口


def encrypt(payload):
    """AES-192-CBC 加密请求体。"""
    raw = json.dumps(payload, separators=(',', ':')).encode()
    pad = 16 - len(raw) % 16
    raw += bytes([pad]) * pad
    ct = AES.new(KEY, AES.MODE_CBC, IV).encrypt(raw)
    return 'request=' + urllib.parse.quote(base64.b64encode(ct).decode(), safe='=').replace('+', '%2B')


def header(action):
    return {
        'url': '//api.kessbet.com/cpop/ots/unct/public/handler',
        'method': 'post',
        'action': action,
        'isApp': 0,
        'brand': 'KYA',
        'cmdName': 'h5_zz',
        'platformVersion': '1.4.5',
        'appVersion': '1.4.5',
        'platformCode': 'pc',
        'token': '',
        'uuid': 'x',
        'zoneId': 'Asia/Shanghai',
    }


def make_session(proxy=None):
    proxy = proxy or os.environ.get('KESSBET_PROXY', DEFAULT_PROXY)
    proxies = {'https': proxy, 'http': proxy} if proxy else None
    return creq.Session(impersonate='chrome', proxies=proxies)


def post(sess, action, body):
    r = sess.post(API, data=encrypt({'header': header(action), 'body': body}),
                  headers=HDRS, timeout=30)
    r.raise_for_status()
    j = r.json()
    if j.get('code') != 1:
        raise RuntimeError(f"接口返回 code={j.get('code')} msg={j.get('msg')}")
    return j['data']


def match_time(m):
    """接口的 mbtp 是本地时间戳；mdt/mbt 是肯尼亚时间（UTC+3），不要用它们做日期判断。"""
    return datetime.datetime.fromtimestamp(m['mbtp'] / 1000)


def sort_key(m):
    return m.get('mbtp', 0)


def filter_today(matches, now=None):
    """按本地日历日筛选今天（含）之后的当天场次。"""
    now = now or datetime.datetime.now()
    day = now.date()
    return [m for m in matches if match_time(m).date() == day]


def fetch_schedule(sess, tab_id=1, max_pages=40):
    """拉全部赛程。列表是游标分页，每页 12 个联赛组。"""
    base = {'tabId': tab_id, 'playType': '', 'timeslot': 'All', 'leagues': []}
    seen = {}
    last_lid = last_mid = ''
    for _ in range(max_pages):
        body = dict(base)
        if last_lid:
            body['lastLeagueId'] = last_lid
            body['lastMatchId'] = last_mid
        data = post(sess, ACT_LIST, body)
        groups = data.get('leagueGroupList') or []
        if not groups:
            break
        new = 0
        for g in groups:
            for m in g['matchList']:
                if m['matchId'] not in seen:
                    seen[m['matchId']] = m
                    new += 1
        if new == 0:
            break
        last_lid = str(groups[-1]['li'])
        last_mid = groups[-1]['matchList'][-1]['matchId']
    return list(seen.values())


def fetch_odds(sess, match_id):
    return post(sess, ACT_MATCH, {'matchId': match_id})


def cmd_list(args):
    sess = make_session(args.proxy)
    matches = fetch_schedule(sess)
    if args.today:
        matches = filter_today(matches)
    matches.sort(key=sort_key)
    print(f'共 {len(matches)} 场')
    by_day = Counter(match_time(m).date() for m in matches)
    for d in sorted(by_day):
        print(f'  {d:%d/%m}  {by_day[d]:>4} 场')
    print()
    for m in matches:
        print(f"{match_time(m):%m-%d %H:%M}  {m['ln'][:32]:32}  "
              f"{m['htn'][:18]:18} vs {m['atn'][:18]:18}  {m['matchId']}")
    if args.out:
        json.dump(matches, open(args.out, 'w', encoding='utf-8'), ensure_ascii=False)
        print(f'\n已保存 -> {args.out}')


def cmd_odds(args):
    sess = make_session(args.proxy)
    data = fetch_odds(sess, args.match_id)
    print(f'matchId {args.match_id}: {len(data)} 个盘口')
    if args.out:
        json.dump(data, open(args.out, 'w', encoding='utf-8'), ensure_ascii=False)
        print(f'已保存 -> {args.out}')
    else:
        for mk in data[:5]:
            print(f"  {mk['mari'].rsplit('|', 1)[-1]}")


def main():
    ap = argparse.ArgumentParser(description='KessBet 数据抓取')
    ap.add_argument('--proxy', default=None, help=f'代理地址，默认 {DEFAULT_PROXY}')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_list = sub.add_parser('list', help='赛程列表')
    p_list.add_argument('--today', action='store_true', help='只看今天')
    p_list.add_argument('-o', '--out', help='保存为 JSON')
    p_list.set_defaults(func=cmd_list)

    p_odds = sub.add_parser('odds', help='单场全盘口')
    p_odds.add_argument('match_id')
    p_odds.add_argument('-o', '--out', help='保存为 JSON')
    p_odds.set_defaults(func=cmd_odds)

    args = ap.parse_args()
    try:
        args.func(args)
    except Exception as e:
        print(f'失败: {e}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
