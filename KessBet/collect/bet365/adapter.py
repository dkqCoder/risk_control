"""collect/bet365/adapter.py — 把 bet365 接入 Adapter 抽象。

取数走 `browser.Bet365Browser`（真实 Chrome + 站点自己的 Loader），
解析走 `league.py`。两者都已验证可用，能拉到联赛整轮的赔率：

    英超 20 场 / 1X2 赔率 / 覆盖成本 1.05~1.09

## 与 KessBet 的差异

  * bet365 的联赛编号（`91422157`）不是比赛编号，需要先拉到联赛才知道有哪些比赛；
  * 比赛 ID 用 `fixture_id`（数字串），跨平台对齐靠 `sportradar_id`；
  * 开赛时间是**英国时间**，入库前要转。
"""
import logging
from datetime import datetime, timedelta

from collect.base import Adapter
from collect.bet365 import league
from collect.bet365.browser import Bet365Browser
from common.models import Match, Selection

log = logging.getLogger(__name__)

PLATFORM = 'bet365'
SOCCER = 'sr:sport:1'

# 已知联赛编号。bet365 用内部编号，没有公开的完整列表
# （leftnavcontentapi/validcompetitions 返回空），只能按需补。
LEAGUES = {
    '91422157': '英格兰超级联赛',
    '135650998': '英格兰冠军联赛',
}

# 英国时间 → 北京时间
UK_TO_BEIJING = timedelta(hours=8)


class Bet365Adapter(Adapter):
    name = PLATFORM

    def __init__(self, proxy: str | None = 'http://127.0.0.1:7897',
                 league_ids: list[str] | None = None):
        self._proxy = proxy
        self._league_ids = league_ids or ['91422157']
        self._browser: Bet365Browser | None = None
        self._fixtures: dict[str, dict] = {}

    # ---------- 浏览器复用 ----------

    def _b(self) -> Bet365Browser:
        """复用同一个浏览器实例——每次新建要 20 秒，绝不能每场都开。"""
        if self._browser is None:
            self._browser = Bet365Browser(proxy=self._proxy, headless=False)
            self._browser.start()
        return self._browser

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------- Adapter 接口 ----------

    def list_matches(self, sport: str) -> list[Match]:
        """拉全部已配置联赛的比赛。sport 只支持足球。"""
        if sport != SOCCER:
            return []
        out = []
        b = self._b()
        for lid in self._league_ids:
            try:
                text = b.fetch_markets(lid)
            except Exception as e:
                log.warning('联赛 %s 拉取失败: %s', lid, e)
                continue
            for fx in league.parse_fixtures(text):
                self._fixtures[fx['fixture_id']] = fx
                out.append(self._to_match(fx))
        return out

    def fetch_odds(self, match_id: str, home: str = '', away: str = '') -> list[Selection]:
        """拉一场比赛在联赛页可见的全部盘口。

        match_id 是 fixture_id（数字串）。为拿齐盘口要逐个请求盘口目录里的 PD，
        所以调用方应尽量批量处理，不要逐场调用。
        """
        fx = self._fixtures.get(match_id)
        if fx is None:
            log.warning('未知比赛 %s，请先 list_matches', match_id)
            return []

        b = self._b()
        out: list[Selection] = []
        for lid in self._league_ids:
            try:
                catalog = b.fetch_markets(lid)
            except Exception:
                continue
            if not any(f['fixture_id'] == match_id for f in league.parse_fixtures(catalog)):
                continue

            for mk in league.parse_market_catalog(catalog):
                try:
                    detail = b.fetch_market_detail(mk['pd'])
                except Exception as e:
                    log.debug('盘口 %s 拉取失败: %s', mk['market'], e)
                    continue
                row = league.build_market_table(detail).get(fx['name'])
                if not row:
                    continue
                for sel, odds in row.items():
                    out.append(Selection(
                        market=mk['market'],
                        side=_side(sel, fx),
                        line='',
                        label=sel,
                        odds=odds,
                    ))
            break
        return out

    # ---------- 内部 ----------

    def _to_match(self, fx: dict) -> Match:
        kick = fx.get('kickoff_uk')
        if kick is not None:
            kick = kick + UK_TO_BEIJING
        else:
            kick = datetime.now()
        return Match(
            platform=PLATFORM,
            match_id=fx['fixture_id'],
            sport=SOCCER,
            league=LEAGUES.get(self._league_ids[0], ''),
            country='',
            home_team=fx['home'],
            away_team=fx['away'],
            kickoff_time=kick,
            sportradar_id=fx['sportradar_id'],
        )


def _side(selection: str, fx: dict) -> str:
    """把选项映射成主/客/中性。"""
    if selection == '1':
        return 'H'
    if selection == '2':
        return 'A'
    return ''
