"""collect/bet365/http.py — bet365 内容接口客户端。

【重要：当前实测状态】

中国出口 IP 无法直接访问 bet365；需要境外代理。即便走代理，也只有
`homepagecontentapi/pod?pd=#HO#P31524#` 能拿到数据（约 260 KB，含
9 场比赛、33 个选项、5 种盘口）。

其余接口（足球页 / 单场全盘口）返回 **HTTP 200 但 0 字节**：

    /splashcontentapi/soccertab      体育页内容（足球全盘口入口）
    /splashcontentapi/getsplashpods   同上，带 tzo 参数
    /matchbettingcontentapi/coupon    单场全盘口
    /matchmarketscontentapi/*         赛程/盘口
    /leftnavcontentapi/validcompetitions  返回 "[]"

这不是参数或 token 问题：真实浏览器同源 fetch 同样拿到 0 字节，
而同一会话下 `homepagecontentapi/pod` 正常返回 260 KB。
判定为服务端按出口 IP 降级——要求代理出口在 bet365 正常运营区。
换到合格的出口后本模块无需改代码。

协议要点：
  * GET，查询串 `lid/zid/cid/cgid/ctid` + `pd`（页面描述符，URL 编码）
  * `cstid/tcstid/crid` 三个参数只有部分接口需要（tzo 系）
  * 不需要额外 token；`x-net-sync-term` 头可省略
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
