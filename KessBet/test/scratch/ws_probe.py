"""探针: 用 curl_cffi 直连 KessBet socket.io v2, 验证订阅与推送完整度。

socket.io v2 (EIO=3) 纯文本协议:
  0{...}          握手
  40              命名空间连接
  42["event",d]   事件
  2               客户端 ping / 3 服务端 pong
"""
import json
import sys
import time

from curl_cffi import WebSocket, requests as creq

WS_URL = ('wss://socketio.kessbet.com/cpop/network/'
          '?brand=KYA&platform=pc&EIO=3&transport=websocket')
API = 'https://api.kessbet.com/cpop/ots/unct/public/handler'

sys.path.insert(0, '.')
import kessbet_client as C  # noqa: E402

PROXY = {'https': 'http://127.0.0.1:7897', 'http': 'http://127.0.0.1:7897'}


def pick_match():
    """挑一场临近开赛的比赛。"""
    s = creq.Session(impersonate='chrome', proxies=PROXY)
    matches = C.fetch_schedule(s)
    now = time.time()
    upcoming = [m for m in matches if m['mbtp'] / 1000 > now]
    upcoming.sort(key=lambda m: m['mbtp'])
    return upcoming[0]


def main():
    m = pick_match()
    mid = m['matchId']
    import datetime
    ko = datetime.datetime.fromtimestamp(m['mbtp'] / 1000)
    print(f'目标比赛: {m["htn"]} vs {m["atn"]}  ({m["ln"]})')
    print(f'  matchId={mid}  开赛={ko:%Y-%m-%d %H:%M}')

    ws = WebSocket()
    ws.connect(WS_URL, proxies=PROXY, impersonate='chrome')
    print('\n已连接，开始接收...')

    frames = []
    bet_frames = []
    sent_watch = False
    t0 = time.time()
    deadline = t0 + float(sys.argv[1] if len(sys.argv) > 1 else 60)

    while time.time() < deadline:
        try:
            msg = ws.recv(timeout=5)
        except Exception as e:
            if 'timeout' in str(e).lower() or 'Timeout' in type(e).__name__:
                continue
            print('recv error:', type(e).__name__, e)
            break
        if not msg:
            continue
        if isinstance(msg, bytes):
            msg = msg.decode('utf-8', 'replace')

        # 握手完成 -> 订阅
        if msg.startswith('40') and not sent_watch:
            payload = json.dumps({'msgContent': mid}, separators=(',', ':'))
            ws.send(f'42["watch_match",{payload}]')
            print(f'>>> 订阅 watch_match: {mid}')
            sent_watch = True
            continue

        # 心跳
        if msg == '2':
            ws.send('3')
            continue
        if msg.startswith('3'):
            continue

        frames.append(msg)
        if msg.startswith('42'):
            try:
                arr = json.loads(msg[2:])
                evt = arr[0]
                print(f'<<< [{evt}] {str(arr[1:])[:120]}')
                if evt == 'bet_change':
                    bet_frames.append(arr[1])
            except Exception:
                print(f'<<< (raw) {msg[:150]}')

    ws.close()

    # ---------- 分析 ----------
    print('\n' + '=' * 60)
    print(f'共收 {len(frames)} 帧, 其中 bet_change {len(bet_frames)} 条')

    if bet_frames:
        all_markets = set()
        for bc in bet_frames:
            bc = json.loads(bc) if isinstance(bc, str) else bc
            for mari in bc.get('update', []) + bc.get('insert', []):
                nm, _ = _parse(mari)
                if nm:
                    all_markets.add(nm)
        print(f'\n推送涉及的盘口种类 ({len(all_markets)} 种):')
        for nm in sorted(all_markets):
            print(f'  - {nm}')

        # 对比: 该场全盘口有多少
        print(f'\n对比：全盘口接口返回 {_count_api(mid)} 个盘口')
    else:
        print('\n未收到 bet_change。可能该场赔率暂无变动，或订阅未生效。')


def _parse(mari):
    import kessbet_arb as P
    return P.parse_mari(mari)


def _count_api(mid):
    s = creq.Session(impersonate='chrome', proxies=PROXY)
    try:
        return len(C.fetch_odds(s, mid))
    except Exception:
        return -1


if __name__ == '__main__':
    main()
