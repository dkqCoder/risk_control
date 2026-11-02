"""collect/base.py — 采集适配器抽象接口。"""
from abc import ABC, abstractmethod
from typing import Callable

from common.models import Match, Selection


class Adapter(ABC):
    name: str = ''

    @abstractmethod
    def list_matches(self, sport: str) -> list[Match]:
        """拉某运动的赛前赛程。sport 用 tsi 标识（如 'sr:sport:1'）。"""

    @abstractmethod
    def fetch_odds(self, match_id: str, home: str = '', away: str = '') -> list[Selection]:
        """拉单场全部盘口。

        home / away 用于拆分队名盘口（'Arsenal Total' -> side='H'）。
        这是唯一的数据来源——推送内容不参与入库。
        """

    def subscribe(self, match_ids: list[str]) -> None:
        """订阅推送信号。不支持的平台抛 NotImplementedError。"""
        raise NotImplementedError

    def on_push(self, callback: Callable[[str], None]) -> None:
        """注册推送回调。回调参数只有 match_id。"""
        raise NotImplementedError
