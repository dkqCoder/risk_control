"""collect/bet365/http.py — bet365 内容接口的**纯 HTTP** 客户端。

⚠️ 重要：本模块只有首页接口能用，其余全部拿不到内容。真正的取数在
`browser.py`（真实 Chrome + 站点自己的 Loader）。

## 实测结论

| 接口 | 纯 HTTP | 说明 |
|---|---|---|
| `homepagecontentapi/pod` | ✅ 200 KB | 首页精选，唯一对外部调用放行的接口 |
| `splashcontentapi/*` | ❌ 200 + 0 字节 | 体育页内容 |
| `contentdata/*` | ❌ 200 + 0 字节 | 联赛/盘口（真正有赔率的接口） |

**空响应不是参数或 token 问题。** 证据：

* 真实 Chrome 里页面自己发的请求正常，同一会话手动 `fetch()` 同一 URL 返回 0；
* 把成功请求的**完整请求头**（含 `X-Net-Sync-Term`）原样重放，能拿到 25 KB——
  但那个 token 与页面内 `Loader` 实例的 `instanceCounter` 绑定，
  换请求发出方就失效；
* 页面内 `new ns_datalib_net.Loader()` 才稳定可用。

所以本模块保留的价值：
  1. 首页精选赔率可以直接 HTTP 拿，比开浏览器轻得多；
  2. 作为诊断工具——`tools/bet365_probe.py --no-browser` 用它证明
     「HTTP 确实被拒」，把问题定位到调用方而不是网络。

## 协议要点

  * GET，查询串 `lid/zid/cid/cgid/ctid` + `pd`（页面描述符，URL 编码）
  * `cstid/tcstid/crid` 只有部分接口需要（tzo 系）
  * cookie 由网关下发（aps03 / __cf_bm / pstk 等），必须带
  * POD 响应是 text/plain 的竖线记录流，不是 JSON
"""
import logging
import os
import time

from curl_cffi import requests as creq

from collect.bet365 import parse

log = logging.getLogger(__name__)

BASE = 'https://www.bet365.com'
POD_PATH = '/homepagecontentapi/pod'

# 页面描述符：#HO# = 首页，#AS#B1# = 足球体育页，#AC#... = 具体比赛
PD_HOME = '#HO#P31524#'
PD_SOCCER = '#AS#B1#K^5#'

DEFAULT_PROXY = os.environ.get('BET365_PROXY', 'http://127.0.0.1:7897')

# 网关 cookie 是必需的；这几个参数决定国家/语言/币种
GATEWAY_PARAMS = {
    'lid': '10',      # 语言（10 = 简体中文）
    'zid': '0',       # 时区
    'cid': '197',     # 币种
    'cgid': '2',      # 国家组
    'ctid': '197',    # 国家
}
TZO_PARAMS = {'cstid': '1', 'tcstid': '1', 'crid': '1'}


class EmptyContentError(RuntimeError):
    """接口返回 200 但内容为空 —— 通常是被按 IP 降级。"""


class Bet365Http:
    def __init__(self, proxy: str | None = None, timeout: float = 30.0):
        self._proxy = proxy if proxy is not None else DEFAULT_PROXY
        self._timeout = timeout
        self._sess: creq.Session | None = None

    # ---------- 会话 ----------

    @property
    def session(self) -> creq.Session:
        """惰性建会话：先访问首页拿网关 cookie，再调内容接口。"""
        if self._sess is None:
            proxies = {'https': self._proxy, 'http': self._proxy} if self._proxy else None
            self._sess = creq.Session(impersonate='chrome', proxies=proxies)
            self._warm_up()
        return self._sess

    def _warm_up(self) -> None:
        r = self._sess.get(BASE + '/', timeout=self._timeout)
        if r.status_code != 200:
            raise RuntimeError(f'bet365 首页不可用: HTTP {r.status_code}（检查代理出口）')
        names = [c.name for c in self._sess.cookies.jar]
        log.debug('bet365 网关 cookie: %s', names)

    # ---------- 内容接口 ----------

    def _get(self, path: str, pd: str, tzo: bool = False) -> str:
        params = dict(GATEWAY_PARAMS)
        if tzo:
            params.update(TZO_PARAMS)
        params['pd'] = pd
        r = self.session.get(
            BASE + path,
            params=params,
            headers={'referer': BASE + '/', 'accept': '*/*'},
            timeout=self._timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(f'{path} → HTTP {r.status_code}')
        return r.text

    def fetch_pod(self, pd: str = PD_HOME) -> str:
        """拉一个页面描述符对应的 pod 文本。空内容抛 EmptyContentError。"""
        text = self._get(POD_PATH, pd)
        if not text.strip():
            raise EmptyContentError(
                f'{POD_PATH} pd={pd} 返回空内容。'
                f'这是出口 IP 被 bet365 降级的典型症状（代理出口需在运营区）。'
            )
        return text

    # ---------- 高层 ----------

    def fetch_home_odds(self) -> dict[str, list[dict]]:
        """首页精选比赛的赔率。当前唯一稳定可用的数据源。"""
        return parse.parse_odds(self.fetch_pod(PD_HOME))

    def fetch_home_events(self) -> list[dict]:
        """首页精选比赛列表。"""
        return parse.parse_events(self.fetch_pod(PD_HOME))

    def probe(self) -> dict:
        """自检：逐个接口探活，返回每个接口的字节数。

        用来判断当前代理出口到底能不能用，不抛异常。
        """
        checks = [
            ('home_pod', POD_PATH, PD_HOME, False),
            ('soccer_tab', '/splashcontentapi/soccertab', PD_SOCCER, False),
            ('soccer_pods', '/splashcontentapi/getsplashpods', PD_SOCCER, True),
        ]
        out = {}
        for name, path, pd, tzo in checks:
            t0 = time.time()
            try:
                text = self._get(path, pd, tzo=tzo)
                out[name] = {'bytes': len(text), 'secs': round(time.time() - t0, 2)}
            except Exception as e:  # 探活不该中断
                out[name] = {'error': f'{type(e).__name__}: {e}'}
        return out
