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


_shot_seq = 0


def _ts(t: float) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))


def _finish(result: dict) -> dict:
    result['capture_finished_at'] = _ts(time.time())
    result['elapsed_sec'] = round(
        time.time() - time.mktime(time.strptime(
            result['capture_started_at'], '%Y-%m-%d %H:%M:%S')), 2)
    return result


def capture_match_meta(match_id: str, out_path: str,
                       width: int = 1500, height: int = 2600) -> dict:
    """截一张平台官网单场页，返回结构化结果（含实际 tab 和页面身份）。

    D03：每次任务使用独立的端口和临时 profile，避免多个 worker 连到
    同一个 CDP 页面/互相导航。调用方应通过 shot_queue 串行调度。
    """
    global _shot_seq
    result = {
        'ok': False, 'path': out_path, 'match_id': match_id,
        'url': MATCH_URL % match_id, 'page_url': None,
        'tab': None, 'coverage': 'none', 'reason': None,
        # 即使点击了 All，也只证明 tab 被点开；没有逐项核对目标选项、
        # 赔率和锁定状态。页面截图始终是辅助证据，不能升级为页面报价已核验。
        'page_verified': False,
        'coverage_note': '需逐项 DOM/网络核对，当前仅记录 tab 与截图',
        'capture_started_at': _ts(time.time()),
        'capture_finished_at': None, 'elapsed_sec': None,
    }
    browser = find_browser()
    if not browser:
        result['reason'] = 'no_browser'
        return _finish(result)

    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    try:
        if os.path.exists(out_path):
            os.remove(out_path)
    except OSError:
        pass

    profile = tempfile.mkdtemp(prefix='kessbet_shot_')
    proc = None
    cdp = None
    try:
        # 端口用 0：让 Chrome 自己选，再读 profile 里的
        # DevToolsActivePort。固定端口会和系统动态端口/并发任务冲突
        # （2026-11-17 实测 9401 连不上，port=0 正常）。
        proc = subprocess.Popen(
            [browser, '--headless=new', '--disable-gpu', '--no-sandbox',
             '--hide-scrollbars', '--remote-debugging-port=0',
             f'--user-data-dir={profile}', f'--window-size={width},{height}',
             'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

        port = None
        port_file = os.path.join(profile, 'DevToolsActivePort')
        for _ in range(80):
            try:
                with open(port_file, encoding='utf-8') as f:
                    port = int(f.readline().strip())
                break
            except (OSError, ValueError):
                time.sleep(0.25)
        if not port:
            result['reason'] = 'cdp_not_ready'
            return _finish(result)

        cdp = _Cdp(port)
        cdp.call('Page.enable')
        cdp.call('Page.navigate', url=result['url'])
        time.sleep(_LOAD_SEC)

        # 页面身份校验：必须真的落在本场比赛的单场页，不能截到首页。
        href = cdp.eval('location.href')
        result['page_url'] = href
        want = result['url'].split('?')[0]
        if not href or not str(href).startswith(want):
            result['reason'] = 'wrong_page'
            return _finish(result)

        # 点 All 而不是 Main。Main 不含团队总进球/Correct Score 等，
        # 点了 Main 的图只能算部分覆盖，不能宣称完整。
        tab = cdp.eval("""
        (() => {
          const pick = name => [...document.querySelectorAll('*')]
            .find(e => e.children.length === 0 &&
                       (e.innerText || '').trim() === name);
          const all = pick('All');
          if (all) { all.click(); return 'all'; }
          const main = pick('Main');
          if (main) { main.click(); return 'main'; }
          return 'no_tab';
        })()
        """)
        result['tab'] = tab
        if tab == 'all':
            result['coverage'] = 'all'
        elif tab == 'main':
            result['coverage'] = 'partial'
            result['reason'] = 'all_tab_missing'
        else:
            result['coverage'] = 'none'
            result['reason'] = 'no_tab'
        log.info('平台截图 tab=%s coverage=%s %s',
                 tab, result['coverage'], match_id)
        time.sleep(_AFTER_CLICK_SEC)

        cdp.shot(out_path)
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 1024:
            # no_tab 时赔率没渲染，图只有盘口名，不能算成功。
            result['ok'] = tab in ('all', 'main')
            if not result['ok']:
                result['reason'] = 'target_not_rendered'
        else:
            result['reason'] = 'empty_file'
    except Exception as e:
        result['reason'] = str(e)
        log.warning('平台截图失败 %s: %s', match_id, e)
    finally:
        if cdp:
            cdp.close()
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        import shutil
        shutil.rmtree(profile, ignore_errors=True)
    return _finish(result)


def capture_match(match_id: str, out_path: str,
                  width: int = 1500,
                  height: int = 2600) -> tuple[bool, str]:
    """兼容旧调用：返回 (成功?, 路径/失败原因)。"""
    r = capture_match_meta(match_id, out_path, width=width, height=height)
    if r['ok']:
        return True, r['path']
    return False, r.get('reason') or 'capture_failed'
