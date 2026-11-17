"""common/scores.py — 比分相关的共享小工具。"""
from __future__ import annotations


def half_time_score(rows) -> tuple[int, int] | None:
    """从赔率流水反推半场比分。

    rows: [(minute, home_score, away_score), ...] 某一场的全部流水行

    取 minute <= 45 的最后一条。取不到返回 None（上半场时本来就不知道）。

    为什么不用 fetch_state 的 msScoreList：
      实测当前比分 1:0 时它返回两个 0:0 的阶段快照，那是「每个阶段的
      比分变化记录」，不是半场比分。用它构造可达终局会算错。

    注意：这是启发式。采集侧会把它冻结进 kessbet_live_snapshot，
    分析和回放都用冻结值，避免不同时刻读到不同历史。
    """
    best = None
    for minute, hs, as_ in rows:
        if minute is None or hs is None or as_ is None:
            continue
        if minute <= 45:
            if best is None or minute >= best[0]:
                best = (minute, int(hs), int(as_))
    return (best[1], best[2]) if best else None
