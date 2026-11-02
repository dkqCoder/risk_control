"""collect/writer.py — 整场替换 + 变动历史写入。

两张表的分工：
  kessbet_odds_prematch  当前状态，一个选项一行（分析侧读取源）
  kessbet_odds_history   变动记录，只增不改（事后复盘），保留 7 天
"""
import logging
from datetime import datetime

from common.timeutil import now_beijing, to_db

log = logging.getLogger(__name__)

UPSERT_SQL = """
INSERT INTO kessbet_odds_prematch
  (platform, match_id, sport, market, side, line, selection,
   odds, kickoff_time, updated_time)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON DUPLICATE KEY UPDATE
  prev_odds   = odds,
  odds        = VALUES(odds),
  kickoff_time = VALUES(kickoff_time),
  updated_time = VALUES(updated_time)
"""

HISTORY_SQL = """
INSERT INTO kessbet_odds_history
  (platform, match_id, sport, market, side, line, selection,
   odds, prev_odds, kickoff_time, changed_time)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
"""


# 字段宽度上限（与 schema.sql 对齐）。
# 超出时截断而不是整场失败——线上遇到过 'Data too long for column market'，
# 一场数据写不进去会让该场永久缺失。
MAX_MARKET = 64
MAX_LINE = 16
MAX_SELECTION = 32

_truncated_warned: set[tuple] = set()


def _fit(value: str, limit: int, field: str, match_id: str) -> str:
    if len(value) <= limit:
        return value
    key = (field, value[:40])
    if key not in _truncated_warned:
        _truncated_warned.add(key)
        log.warning('truncating %s (len=%d) for %s: %r',
                    field, len(value), match_id, value[:80])
    return value[:limit]


def replace_match(dao, match, selections, now: datetime | None = None) -> dict:
    """整场替换：写历史（只记变化）+ upsert 当前状态 + 删除接口里没有的行。

    数据源是 HTTP 全量接口，所以"接口里没有" == "平台上真的没有了"。
    直接物理删除是安全的，不需要软删除标记。

    返回 {'written': int, 'changed': int}；已开赛则返回全 0 且不写。
    """
    # 统一转成 naive 北京时间再比较——调用方可能传 aware 或 naive
    now = to_db(now or now_beijing())
    kickoff = to_db(match.kickoff_time)

    empty = {'written': 0, 'changed': 0}
    if now >= kickoff:
        return empty
    if not selections:
        return empty

    changed_at = now

    conn = dao.conn
    try:
        # 1. 读当前值，找出真正变化的
        current = {
            (m, s, l, lab): odds
            for m, s, l, lab, odds in dao.get_selections(match.match_id)
        }

        history_rows = []
        for s in selections:
            old = current.get(s.key)
            if old is None or abs(old - s.odds) > 1e-9:
                history_rows.append((
                    'kessbet', match.match_id, match.sport,
                    _fit(s.market, MAX_MARKET, 'market', match.match_id),
                    s.side,
                    _fit(s.line, MAX_LINE, 'line', match.match_id),
                    _fit(s.label, MAX_SELECTION, 'selection', match.match_id),
                    s.odds, old, kickoff, changed_at,
                ))

        # 2. 写历史（只增）
        if history_rows:
            with conn.cursor() as cur:
                cur.executemany(HISTORY_SQL, history_rows)

        # 3. 写当前状态（覆盖）
        rows = [(
            'kessbet', match.match_id, match.sport,
            _fit(s.market, MAX_MARKET, 'market', match.match_id),
            s.side,
            _fit(s.line, MAX_LINE, 'line', match.match_id),
            _fit(s.label, MAX_SELECTION, 'selection', match.match_id),
            s.odds, kickoff, changed_at,
        ) for s in selections]
        with conn.cursor() as cur:
            cur.executemany(UPSERT_SQL, rows)

        # 4. 删除接口里没有的行
        #    注意：key 必须用截断后的值，否则和库里存的（已截断）对不上
        fresh_keys = {(
            _fit(s.market, MAX_MARKET, 'market', match.match_id),
            s.side,
            _fit(s.line, MAX_LINE, 'line', match.match_id),
            _fit(s.label, MAX_SELECTION, 'selection', match.match_id),
        ) for s in selections}
        db_keys = dao.fetch_keys(match.match_id)
        stale = db_keys - fresh_keys
        if stale:
            dao.delete_keys(match.match_id, stale)

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {'written': len(rows), 'changed': len(history_rows)}
