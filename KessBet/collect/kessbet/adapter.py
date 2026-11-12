"""collect/kessbet/adapter.py — 把 http / ws / parse 组合成 Adapter。"""
import logging

from common.models import Match, Selection
from collect.base import Adapter
from collect.kessbet.http import KessbetHttp, ForbiddenError
from collect.kessbet.ws import KessbetWs

log = logging.getLogger(__name__)


class KessbetAdapter(Adapter):
    name = 'kessbet'

    def __init__(self, proxy: str | None = None):
        self._http = KessbetHttp(proxy=proxy)
        self._ws: KessbetWs | None = None
        self._on_change = None

    # ---------- Adapter 接口 ----------

    def list_matches(self, sport: str) -> list[Match]:
        return self._http.list_matches(sport)

    def list_live_matches(self, sport: str) -> list[Match]:
        """当前正在滚球的比赛。

        和 list_matches 是两个不同的平台接口 —— 赛程接口(00252)不返回
        已经开赛的比赛，见 KessbetHttp.live_list_raw 的说明。
        """
        return self._http.list_live_matches(sport)

    def fetch_odds(self, match_id: str, home: str = '', away: str = '') -> list[Selection]:
        return self._http.fetch_odds(match_id, home, away)

    def fetch_state(self, match_id: str) -> dict | None:
        """比赛实时状态（比分/时间/红黄牌/角球）。

        滚球采集的必要输入 —— 盘口接口只给赔率，不给比分。
        比赛结束或解析失败返回 None。
        """
        return self._http.fetch_state(match_id)

    # ---------- 推送 ----------

    def on_push(self, callback) -> None:
        self._on_change = callback

    def start_ws(self, initial_ids: list[str] | None = None) -> KessbetWs:
        """建立并启动 WebSocket 连接。"""
        if self._on_change is None:
            raise RuntimeError('on_push() must be called before start_ws()')
        self._ws = KessbetWs(on_change=self._on_change)
        self._ws.start(initial_ids=initial_ids)
        return self._ws

    def ws(self) -> KessbetWs:
        if self._ws is None:
            raise RuntimeError('ws not started')
        return self._ws
