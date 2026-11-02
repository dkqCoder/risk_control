"""common/models.py — 跨模块共享的数据模型。"""
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Match:
    """一场比赛的静态信息。

    kickoff_time 是 naive 北京时间（见 common/timeutil.py）。
    """
    platform: str
    match_id: str
    sport: str                 # 'sr:sport:1'
    league: str
    country: str
    home_team: str
    away_team: str
    kickoff_time: datetime
    sportradar_id: str = ''

    @property
    def key(self) -> tuple:
        return (self.platform, self.match_id)


@dataclass(frozen=True)
class Selection:
    """一个盘口选项。平台无关的统一表示。"""
    market: str                # 'Total' / '1st Half - Total'
    side: str                  # '' / 'H' / 'A'
    line: str                  # '2.5' / '0:1' / '2up' / ''
    label: str                 # 'over' / 'X & yes' / '2:1'
    odds: float

    @property
    def key(self) -> tuple:
        return (self.market, self.side, self.line, self.label)


@dataclass(frozen=True)
class RawMarket:
    """接口返回的单个盘口原始对象（只保留需要的字段）。"""
    mari: str
    market_id: str
    category: str = ''         # g 字段：分类路径
    rule_text: str = ''        # d 字段：规则说明
