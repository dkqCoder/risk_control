"""collect/bet365/browser.py — 用真实 Chrome 走站点自己的 Loader 取数据。

## 为什么必须用浏览器

bet365 的体育内容接口（`/contentdata/...`）不对外部调用方开放：

| 调用方式 | 结果 |
|---|---|
| 纯 HTTP + 正确 cookie | 200 但 0 字节 |
| 页面内裸 `fetch()` 同一 URL | 200 但 0 字节 |
| 页面内用站点自己的 `Loader` 类 | ✅ 正常返回 |

原因是 `Loader.load()` 内部会走 `xcft()` → 向页面派发 `xcftr` 事件 →
由页面内的混淆脚本回填 `xcft<instanceCounter>` 事件 → 拿到与**该实例**
绑定的 `X-Net-Sync-Term` token。这个 token：

* 与 `Loader.instanceCounter`（每次 new 就自增）绑定；
* 不在页面里主动触发就拿不到；
* 裸 fetch 不经过这套流程，所以是空响应。

因此唯一可靠的做法是：开一个真实 Chrome，在页面上下文里 `new ns_datalib_net.Loader()`
发请求。本模块封装这件事。

## 已验证的边界

* 真实 Chrome 必须**不带** `--enable-automation`（`navigator.webdriver` 为 true 时
  足球页渲染不出内容）。
* 需要先点掉 cookie 弹窗，否则页面内脚本不初始化。
* 接口返回的仍是竖线分隔文本，解析交给 `parse.py`。

## 接口链路

```
① 盘口目录  /contentdata/matchmarketscontentapi/markets?pd=#AC#B1#C1#D1002#E{联赛}#G40#H^1#
              → MA 记录，NA=盘口名，PD=该盘口的页面路径
② 单盘口    /contentdata/matchmarketscontentapi/markets?pd=<上一步的 PD>
              → PA 记录，OD=英式分数赔率
```
"""
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

log = logging.getLogger(__name__)

DEFAULT_CHROME = r'C:\Program Files\Google\Chrome\Application\chrome.exe'
FALLBACK_CHROME = [
    DEFAULT_CHROME,
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    os.path.expanduser(r'~\AppData\Local\Google\Chrome\Application\chrome.exe'),
]

GATEWAY_PARAMS = 'lid=10&zid=0&cid=197&cgid=2&ctid=197'
MARKETS_PATH = '/contentdata/matchmarketscontentapi/markets'
MATCH_BETTING_PATH = '/contentdata/matchbettingcontentapi/coupon'

# 单场比赛的标签页编号。实测哪些有内容：
#   1 Popular / 3 Asian Lines / 6 Goals / 7 Half / 18 Outcome / 19 Specials
MATCH_TABS = (1, 3, 6, 7, 18, 19)

# 联赛页描述符模板：E{id} 是 bet365 的联赛编号
PD_LEAGUE = '#AC#B1#C1#D1002#E{league_id}#G40#H^1#'


def _find_chrome() -> str:
    for p in FALLBACK_CHROME:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError('找不到 Chrome，请传 chrome_path')


def _free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Bet365Browser:
    """常驻的真实 Chrome 会话。

    用法:
        with Bet365Browser(proxy='http://127.0.0.1:7897') as b:
            catalog = b.fetch_markets('E91422157')
    """

    def __init__(self, proxy: str | None = 'http://127.0.0.1:7897',
                 chrome_path: str | None = None, headless: bool = False,
                 start_url: str = 'https://www.bet365.com/',
                 activation_league: str = '91422157'):
        self.proxy = proxy
        self.chrome_path = chrome_path or _find_chrome()
        self.headless = headless
        self.start_url = start_url
        # 激活用的联赛（英超）。只用来让页面进入「已活动」状态
        self.activation_league = activation_league
        self._proc: subprocess.Popen | None = None
        self._ws = None
        self._id = 0
        self._profile: str | None = None

    # ---------- 生命周期 ----------

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self) -> None:
        port = _free_port()
        self._profile = tempfile.mkdtemp(prefix='bet365-profile-')
        args = [
            self.chrome_path,
            f'--remote-debugging-port={port}',
            f'--user-data-dir={self._profile}',
            '--no-first-run',
            '--no-default-browser-check',
            # 关键：不要加 --enable-automation / --headless=new 的自动化标记
            '--disable-blink-features=AutomationControlled',
            '--window-size=1600,1000',
        ]
        if self.proxy:
            args.append(f'--proxy-server={self.proxy}')
        if self.headless:
            args.append('--headless=new')
        args.append(self.start_url)

        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ws_url = self._wait_debugger(port)
        import websocket  # 延迟导入，未装时只在真正用浏览器时失败
        self._ws = websocket.create_connection(ws_url, timeout=60)
        self._cdp('Page.enable')
        self._cdp('Runtime.enable')
        self._wait_ready()

    def close(self) -> None:
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        finally:
            self._ws = None
            if self._proc:
                self._proc.terminate()
                self._proc = None
            if self._profile and os.path.isdir(self._profile):
                import shutil
                shutil.rmtree(self._profile, ignore_errors=True)

    def _wait_debugger(self, port: int, timeout: float = 40.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = json.load(urllib.request.urlopen(
                    f'http://127.0.0.1:{port}/json', timeout=2))
                pages = [d for d in data if d.get('type') == 'page']
                if pages:
                    return pages[0]['webSocketDebuggerUrl']
            except Exception:
                pass
            time.sleep(0.4)
        raise RuntimeError('Chrome 调试端口未就绪')

    def _cdp(self, method: str, params: dict | None = None, timeout: int = 30) -> dict:
        self._id += 1
        mid = self._id
        self._ws.send(json.dumps({'id': mid, 'method': method, 'params': params or {}}))
        while True:
            msg = json.loads(self._ws.recv())
            if msg.get('id') == mid:
                return msg.get('result', {})

    def js(self, expression: str, await_promise: bool = True, timeout: int = 30):
        r = self._cdp('Runtime.evaluate', {
            'expression': expression,
            'returnByValue': True,
            'awaitPromise': await_promise,
            'timeout': timeout * 1000,
        }, timeout=timeout + 5)
        if r.get('exceptionDetails'):
            raise RuntimeError('页面脚本异常: ' +
                               str(r['exceptionDetails'].get('text'))[:200])
        return r.get('result', {}).get('value')

    def _wait_ready(self, timeout: float = 60.0) -> None:
        """等页面初始化完成。

        实测有两个必要条件，缺一个 Loader 就返回 0 字节：

        1. `ns_datalib_net.Loader` 已定义
        2. 页面**已经发生过一次导航活动**（切过一次路由）

        第 2 点是踩出来的：刚打开首页时直接 new Loader 请求，服务端一律返回
        200 + 0 字节；先把 hash 切到一个联赛页、等它渲染出赔率，之后同一个
        Loader 就能稳定返回 25 KB。看起来 token 流程要等站点的路由/推送层
        启动完毕才生效。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ok = self.js("typeof ns_datalib_net !== 'undefined' && !!ns_datalib_net.Loader",
                             timeout=5)
                if ok:
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('页面未初始化（ns_datalib_net.Loader 不可用）')

        # 点掉 cookie 弹窗；没有就跳过
        self.js("""(() => {
            const b = [...document.querySelectorAll('button,div[role="button"],a')]
                .find(e => /全部接受|Accept all|Aceptar todo|Alle akzeptieren/i.test((e.innerText||'').trim()));
            if (b) { b.click(); return 'clicked'; }
            return 'none';
        })()""")
        time.sleep(2)

        # 触发一次路由活动，激活 token 流程
        self._activate()

    def _activate(self, timeout: float = 45.0) -> None:
        """切到一个联赛页并等它渲染，之后 Loader 才能取到数据。

        注意：这里必须用 `Page.navigate` 走真正的路由跳转，不能用
        `location.hash = ...`。bet365 的路由是 Remix Router 管的，
        直接改 hash 只会得到一个畸形 URL（`#/ACB1C1...`），页面不会重建。
        """
        url = (f'https://www.bet365.com/#/AC/B1/C1/D1002/'
               f'E{self.activation_league}/G40/H%5E1/')
        self._cdp('Page.navigate', {'url': url})
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2)
            try:
                odds = self.js(r'(document.body.innerText.match(/[0-9]+\.[0-9]{2}/g)||[]).length',
                               timeout=10)
            except Exception:
                odds = 0
            if odds and odds > 20:
                log.debug('页面已激活，检测到 %s 个赔率', odds)
                return
        raise RuntimeError('页面激活失败：联赛页未渲染出赔率')

    # ---------- 取数 ----------

    def fetch_via_loader(self, path: str, timeout: int = 30) -> str:
        """在页面里用站点自己的 Loader 请求 path，返回原始文本。

        这是唯一能拿到内容的方式——裸 fetch 会被拒（返回空）。
        """
        expr = f"""(async () => new Promise(res => {{
            let done = false;
            const finish = v => {{ if (!done) {{ done = true; res(v); }} }};
            let L;
            try {{ L = new ns_datalib_net.Loader(); }}
            catch (e) {{ return finish('__ERR__ ctor ' + e.message); }}
            L.completeHandler = (text, status) => finish(status === 200 ? String(text || '') : '__ERR__ status ' + status);
            L.errorHandler = (code, msg) => finish('__ERR__ ' + code + ' ' + msg);
            try {{ L.load({json.dumps(path)}); }}
            catch (e) {{ finish('__ERR__ load ' + e.message); }}
            setTimeout(() => finish('__ERR__ timeout'), {timeout * 1000});
        }}))()"""
        out = self.js(expr, timeout=timeout + 5)
        if out is None:
            raise RuntimeError('Loader 无返回')
        if str(out).startswith('__ERR__'):
            raise RuntimeError(str(out)[8:])
        return str(out)

    def fetch_markets(self, league_id: str) -> str:
        """拉一个联赛的盘口目录（含整轮比赛的 1X2 等盘口）。

        league_id 是不带 E 前缀的数字串，'91422157'；带 E 也接受。
        """
        lid = league_id[1:] if league_id.startswith('E') else league_id
        pd = PD_LEAGUE.format(league_id=lid)
        import urllib.parse
        q = urllib.parse.urlencode({'pd': pd})
        return self.fetch_via_loader(f'{MARKETS_PATH}?{GATEWAY_PARAMS}&{q}')

    def fetch_market_detail(self, pd_path: str) -> str:
        """按 PD 路径拉单个盘口的赔率。"""
        import urllib.parse
        q = urllib.parse.urlencode({'pd': pd_path})
        return self.fetch_via_loader(f'{MARKETS_PATH}?{GATEWAY_PARAMS}&{q}')

    # ---------- 单场比赛全盘口 ----------

    def fetch_match_tab(self, match_id: str, tab: int = 1) -> str:
        """拉单场比赛某个标签页的全部盘口。

        match_id 是比赛的 FI 编号（如 203186235），**不带 E 前缀**。

        标签页编号实测：
            1   Popular      主列表（22 个盘口）
            3   Asian Lines  亚盘 / 球线（8 个盘口）
            6   Goals        大小球相关（40 个盘口）
            7   Half         半场盘（27 个盘口）
            18  Outcome      赛果类（10 个盘口，含半/全场）
            19  Specials     特殊盘口（7 个）
        """
        mid = match_id[1:] if match_id.startswith('E') else match_id
        pd = (f'#AC#B1#C1#D8#E{mid}#F3#I{tab}#')
        import urllib.parse
        q = urllib.parse.urlencode({'pd': pd})
        return self.fetch_via_loader(f'{MATCH_BETTING_PATH}?{GATEWAY_PARAMS}&{q}')

    def fetch_match_all(self, match_id: str, tabs: tuple[int, ...] | None = None,
                        gap: float = 1.5, retries: int = 2) -> dict[int, str]:
        """拉多个标签页，返回 {tab: 原始文本}。

        **必须带间隔**：连续调 6 个 tab 不 sleep 的话，第 2 个起全部返回空——
        服务端对这个接口有短时频率限制。`gap` 默认 1.5 秒，实测够用。
        空响应会重试 `retries` 次。
        """
        out = {}
        for tab in (tabs or MATCH_TABS):
            for attempt in range(retries + 1):
                try:
                    text = self.fetch_match_tab(match_id, tab)
                except Exception as e:
                    log.warning('tab I%s 拉取失败 (%s): %s', tab, type(e).__name__, e)
                    text = ''
                if text and text.strip():
                    out[tab] = text
                    break
                if attempt < retries:
                    time.sleep(gap * 2)
            time.sleep(gap)
        return out
