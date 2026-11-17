"""common/phase.py — 比赛阶段与半场来源分级（F05）。

分钟数只作辅助：补时可能让上半场分钟 >45。平台状态码 `ms` 是主依据，
但 `ms=6/7` 加时钟推出来的“上下半场”仍然只是推断，不能标成官方。
"""
from __future__ import annotations

PHASES = ('first_half', 'halftime', 'second_half', 'finished', 'unknown')

# 实测足球状态码：6/7 进行中、31 中场、100 常规结束、120 点球结束。
_FINISHED = (100, 120)
_HALFTIME = (31,)
_IN_PLAY = (6, 7)


def classify_phase(state: dict) -> tuple[str, str]:
    """返回 (phase, source)。source: official_state | inferred。

    - 100/120 → finished, official_state
    - 31      → halftime, official_state
    - 6/7 有时钟 → first_half/second_half, inferred
    - 6/7 无时钟 → unknown, inferred
    - 其他     → unknown, inferred
    """
    ms = state.get('ms')
    if ms in _FINISHED:
        return 'finished', 'official_state'
    if ms in _HALFTIME:
        return 'halftime', 'official_state'
    if ms in _IN_PLAY:
        mt = state.get('mt')
        if not mt or ':' not in str(mt):
            return 'unknown', 'inferred'
        try:
            minute = int(str(mt).split(':', 1)[0])
        except (TypeError, ValueError):
            return 'unknown', 'inferred'
        return ('first_half' if minute <= 45 else 'second_half'), 'inferred'
    return 'unknown', 'inferred'


def ht_source(phase: str) -> str:
    """半场比分来源。v1 没有权威字段，只可能是启发式或未知。"""
    if phase in ('second_half', 'halftime', 'finished'):
        return 'inferred_history'
    return 'unknown'
