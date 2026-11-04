"""collect/bet365/adapter.py — 把 bet365 内容接口接入 Adapter 抽象。

⚠️ 能力边界（实测，见 http.py 文件头）：

    list_matches()  仅首页精选（约 9 场），**不是**全量赛程
    fetch_odds()    仅首页精选比赛的少量盘口，**不是**单场全盘口

全量赛程和单场全盘口需要 `/splashcontentapi/*` 与
`/matchbettingcontentapi/coupon`，这两个接口在当前代理出口下返回 0 字节。
换到合格出口后，只需把 `list_matches` / `fetch_odds` 换成对应接口，
解析层（parse.py）复用——pod 格式全站一致。
"""
import logging
from datetime import datetime

from collect.base import Adapter
from collect.bet365.http import Bet365Http
from common.models import Match, Selection

log = logging.getLogger(__name__)

PLATFORM = 'bet365'
SOCCER = 'sr:sport:1'


class Bet365Adapter(Adapter):
    name = PLATFORM

    def __init__(self, proxy: str | None = None):
        self._http = Bet365Http(proxy=proxy)
        self._events: list[dict] | None = None

    def list_matches(self, sport: str) -> list[Match]:
        """首页精选比赛。sport 只支持足球（sr:sport:1）。"""
        if sport != SOCCER:
            return []
        events = self._http.fetch_home_events()
        self._events = events
        now = datetime.now()
        out = []
        for e in events:
            out.append(Match(
                platform=PLATFORM,
                # 比赛名当作 ID：首页接口不返回 matchId，只有名字可用
                match_id=e['name'],
                sport=SOCCER,
                league=e['competition'],
                country='',
                home_team=e['home'],
                away_team=e['away'],
                kickoff_time=now,
            ))
        return out

    def fetch_odds(self, match_id: str, home: str = '', away: str = '') -> list[Selection]:
        """返回某场比赛在首页接口里出现的选项。match_id 用 '主队 v 客队' 格式。"""
        odds = self._http.fetch_home_odds()
        rows = odds.get(match_id)
        if not rows:
            return []
        out = []
        for r in rows:
            line = r['line']
            out.append(Selection(
                market=r['market'],
                side=_side(r, home, away),
                line=line,
                label=r['selection'],
                odds=r['odds'],
            ))
        return out


def _side(row: dict, home: str, away: str) -> str:
    """把选项映射成主/客/中性。

    1X2 这类盘口的选择项是队名，EX 字段放对手名；据此判断主客。
    其它盘口（大小球、双方得分）返回空串。
    """
    sel = row['selection']
    opp = row['opponent']
    if home and away:
        if sel == home and opp == away:
            return 'H'
        if sel == away and opp == home:
            return 'A'
    return ''
