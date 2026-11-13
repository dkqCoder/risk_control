"""common/platform_shot.py — 截平台官网某场比赛的滚球页面。

## 页面地址

    https://www.kessbet.com/match/<matchId>

这是平台的单场详情页（滚球时就是滚球盘）。注意：
  - `/live/<id>` 会跳回首页，不是详情页
  - 详情页**没有专用的赛果行**，赔率在「Main / Goals / Total」这些 tab 里

## 为什么必须用 CDP 而不是 --screenshot

两个原因：
  1. `--virtual-time-budget` 在这种带 WebSocket 的页面上永远等不到「网络空闲」，
     命令会一直挂着不返回。
  2. 页面刚打开时赔率是空的，文本里只有盘口名（`Total / over / under / 3.5`），
     没有具体数字。点一下 `Main` tab 之后赔率才渲染出来。

所以走 CDP：navigate → 等 → 点 tab → 等 → captureScreenshot。

## 失败不抛异常

截图是辅助证据，不能影响套利判定。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
import time

log = logging.getLogger(__name__)

BROWSERS = [
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
]

MATCH_URL = 'https://www.kessbet.com/match/%s'

# 页面首屏渲染 + 赔率加载的时间。实测 10 秒够，留 15 秒余量。
_LOAD_SEC = 15
_AFTER_CLICK_SEC = 4


def find_browser() -> str | None:
    for p in BROWSERS:
        if os.path.isfile(p):
            return p
    return None


class _Cdp:
    """极简 CDP 客户端，只用到 navigate / evaluate / screenshot。"""

    def __init__(self, port: int):
        import requests
        import websocket

        targets = requests.get(f'http://127.0.0.1:{port}/json',
                               timeout=5).json()
        page = next((t for t in targets if t['type'] == 'page'), None)
        if page is None:
            raise RuntimeError('没有可用的 page target')
        self._ws = websocket.create_connection(page['webSocketDebuggerUrl'],
                                               timeout=30)
        self._n = 0

    def call(self, method: str, **params):
        self._n += 1
        self._ws.send(json.dumps({'id': self._n, 'method': method,
                                  'params': params}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get('id') == self._n:
                if 'error' in msg:
                    raise RuntimeError(msg['error'])
                return msg.get('result', {})

    def eval(self, expr: str):
        r = self.call('Runtime.evaluate', expression=expr,
                      returnByValue=True, awaitPromise=True)
        return r.get('result', {}).get('value')

    def shot(self, path: str) -> None:
        """只截可视区。

        ⚠️ 不能用 captureBeyondViewport=True —— 实测那样截出来
        中间一大片空白（内容在 DOM 里、innerText 也能读到，但合成层
        没被展开）。视口截图配合足够大的窗口高度才正常。
        """
        r = self.call('Page.captureScreenshot', format='png')
        with open(path, 'wb') as f:
            f.write(base64.b64decode(r['data']))

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def capture_match(match_id: str, out_path: str,
                  width: int = 1500, height: int = 1400) -> tuple[bool, str]:
    """截一张平台官网的单场滚球页。返回 (成功?, 说明)。"""
    browser = find_browser()
    if not browser:
        return False, 'no_browser'

    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    try:
        if os.path.exists(out_path):
            os.remove(out_path)
    except OSError:
        pass

    port = 9333
    profile = os.path.join(tempfile.gettempdir(), 'kessbet_shot_profile')
    proc = subprocess.Popen(
        [browser, '--headless=new', '--disable-gpu', '--no-sandbox',
         '--hide-scrollbars', f'--remote-debugging-port={port}',
         f'--user-data-dir={profile}', f'--window-size={width},{height}',
         'about:blank'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    cdp = None
    try:
        import requests
        for _ in range(60):
            try:
                if requests.get(f'http://127.0.0.1:{port}/json/version',
                                timeout=1).ok:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        else:
            return False, 'cdp_not_ready'

        cdp = _Cdp(port)
        cdp.call('Page.enable')
        cdp.call('Page.navigate', url=MATCH_URL % match_id)
        time.sleep(_LOAD_SEC)

        # 点一下 Main tab —— 不点的话赔率不渲染，截出来只有盘口名
        cdp.eval("""
        (() => {
          const el = [...document.querySelectorAll('*')]
            .filter(e => e.children.length === 0 &&
                         (e.innerText || '').trim() === 'Main');
          if (el.length) { el[0].click(); return 'clicked'; }
          return 'no_tab';
        })()
        """)
        time.sleep(_AFTER_CLICK_SEC)

        cdp.shot(out_path)
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 1024:
            return True, out_path
        return False, 'empty_file'
    except Exception as e:
        log.warning('平台截图失败 %s: %s', match_id, e)
        return False, str(e)
    finally:
        if cdp:
            cdp.close()
        try:
            proc.terminate()
        except Exception:
            pass
