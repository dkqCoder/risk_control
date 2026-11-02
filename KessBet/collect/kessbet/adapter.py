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

    def fetch_odds(self, match_id: str, home: str = '', away: str = '') -> list[Selection]:
        return self._http.fetch_odds(match_id, home, away)

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
