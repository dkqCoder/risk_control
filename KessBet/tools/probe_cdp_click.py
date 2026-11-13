"""用 CDP 驱动 Chrome，展开某场比赛的全部盘口后截图。

## 为什么要 CDP

平台没有单场独立 URL（`/live/<id>` 会跳回首页），赔率在列表里，
要点 `+N Markets >` 才展开。普通的 `--screenshot` 截不到点击后的状态。

## 做法

1. 启动 Chrome，开 `--remote-debugging-port`
2. 连 websocket，用 `Runtime.evaluate` 执行 JS 找元素并点击
3. 等待渲染，`Page.captureScreenshot` 截图

用法：
    py tools/probe_cdp_click.py "Asc Hokkaido"
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time

import requests
import websocket

CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
PORT = 9333


def start_chrome():
    user_dir = os.path.join(tempfile.gettempdir(), 'cdp_profile_live')
    proc = subprocess.Popen([
        CHROME,
        '--headless=new',
        '--disable-gpu',
        '--no-sandbox',
        '--hide-scrollbars',
        f'--remote-debugging-port={PORT}',
        f'--user-data-dir={user_dir}',
        '--window-size=1500,1400',
        'about:blank',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for _ in range(40):
        try:
            r = requests.get(f'http://127.0.0.1:{PORT}/json/version', timeout=1)
            if r.ok:
                return proc
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError('Chrome CDP 没起来')


class Cdp:
    def __init__(self):
        r = requests.get(f'http://127.0.0.1:{PORT}/json', timeout=5).json()
        page = next(t for t in r if t['type'] == 'page')
        self.ws = websocket.create_connection(page['webSocketDebuggerUrl'],
                                              timeout=30)
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({'id': self.n, 'method': method,
                                 'params': params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get('id') == self.n:
                if 'error' in msg:
                    raise RuntimeError(msg['error'])
                return msg.get('result', {})

    def eval(self, expr):
        r = self.call('Runtime.evaluate', expression=expr,
                      returnByValue=True, awaitPromise=True)
        return r.get('result', {}).get('value')

    def goto(self, url, wait=6):
        self.call('Page.enable')
        self.call('Page.navigate', url=url)
        time.sleep(wait)

    def shot(self, path):
        r = self.call('Page.captureScreenshot', format='png',
                      captureBeyondViewport=True)
        with open(path, 'wb') as f:
            f.write(base64.b64decode(r['data']))


def main():
    team = sys.argv[1] if len(sys.argv) > 1 else 'Asc Hokkaido'
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        tempfile.gettempdir(), 'cdp_match.png')

    proc = start_chrome()
    try:
        c = Cdp()
        c.goto('https://www.kessbet.com/live', wait=10)

        # 找包含该队名的比赛行，点它里面的 "+N Markets"
        clicked = c.eval(f"""
        (() => {{
          const t = {json.dumps(team)};
          const btns = [...document.querySelectorAll('*')]
            .filter(e => e.children.length === 0 &&
                         /\\+\\d+\\s*Markets?/i.test(e.textContent || ''));
          for (const b of btns) {{
            let p = b;
            for (let i = 0; i < 8 && p; i++) {{
              if ((p.textContent || '').includes(t)) {{
                b.click();
                return 'clicked: ' + b.textContent.trim();
              }}
              p = p.parentElement;
            }}
          }}
          return 'not found (' + btns.length + ' market buttons)';
        }})()
        """)
        print('点击结果:', clicked)
        time.sleep(3)
        c.shot(out)
        print('截图:', out, os.path.getsize(out) if os.path.isfile(out) else 0)
    finally:
        proc.terminate()


if __name__ == '__main__':
    main()
