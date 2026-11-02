"""db/dao.py cleanup 的测试。用真实 MySQL，插入测试数据后清理。"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from db.dao import Dao

BEIJING = timezone(timedelta(hours=8))
PREFIX = '__TEST_CLEANUP__'


def _now():
    return datetime.now(BEIJING).replace(tzinfo=None)


def _insert_match(dao, match_id, kickoff):
    """插入一条 match + 一条 prematch + 一条 history。"""
    with dao.conn.cursor() as c:
        c.execute("""INSERT INTO kessbet_match
                     (platform, match_id, sport, league, country,
                      home_team, away_team, kickoff_time,
                      first_seen_time, updated_time)
                     VALUES ('kessbet',%s,'sr:sport:1','','','A','B',%s,%s,%s)
                     ON DUPLICATE KEY UPDATE kickoff_time=VALUES(kickoff_time)""",
                  (match_id, kickoff, kickoff, kickoff))
        c.execute("""INSERT INTO kessbet_odds_prematch
                     (platform, match_id, sport, market, side, line,
                      selection, odds, kickoff_time, updated_time)
                     VALUES ('kessbet',%s,'sr:sport:1','1x2','','','1',2.0,%s,%s)""",
                  (match_id, kickoff, kickoff))
        c.execute("""INSERT INTO kessbet_odds_history
                     (platform, match_id, sport, market, side, line,
                      selection, odds, kickoff_time, changed_time)
                     VALUES ('kessbet',%s,'sr:sport:1','1x2','','','1',2.0,%s,%s)""",
                  (match_id, kickoff, kickoff))
    dao.conn.commit()


def _cleanup_test_rows(dao):
    with dao.conn.cursor() as c:
        for t in ('kessbet_odds_prematch', 'kessbet_odds_history', 'kessbet_match'):
            c.execute(f"DELETE FROM {t} WHERE match_id LIKE %s", (PREFIX + '%',))
    dao.conn.commit()


@pytest.fixture
def dao():
    d = Dao()
    _cleanup_test_rows(d)
    yield d
    _cleanup_test_rows(d)
    d.close()


def test_cleanup_removes_old_data(dao):
    """超过 7 天的数据被删除。"""
    now = _now()
    old = now - timedelta(days=8)
    fresh = now + timedelta(hours=5)

    _insert_match(dao, PREFIX + 'old', old)
    _insert_match(dao, PREFIX + 'fresh', fresh)

    r = dao.cleanup()

    with dao.conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM kessbet_match WHERE match_id=%s",
                  (PREFIX + 'old',))
        assert c.fetchone()[0] == 0, '8 天前的比赛应被删除'
        c.execute("SELECT COUNT(*) FROM kessbet_odds_prematch WHERE match_id=%s",
                  (PREFIX + 'old',))
        assert c.fetchone()[0] == 0, '8 天前的赔率应被删除'
        c.execute("SELECT COUNT(*) FROM kessbet_odds_history WHERE match_id=%s",
                  (PREFIX + 'old',))
        assert c.fetchone()[0] == 0, '8 天前的历史应被删除'

        c.execute("SELECT COUNT(*) FROM kessbet_match WHERE match_id=%s",
                  (PREFIX + 'fresh',))
        assert c.fetchone()[0] == 1, '未来的比赛不应被删'


def test_cleanup_keeps_data_within_7_days(dao):
    """6 天前开赛的数据要保留（旧的 6 小时策略会删掉）。"""
    now = _now()
    six_days_ago = now - timedelta(days=6)

    _insert_match(dao, PREFIX + 'sixdays', six_days_ago)
    dao.cleanup()

    with dao.conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM kessbet_odds_prematch WHERE match_id=%s",
                  (PREFIX + 'sixdays',))
        assert c.fetchone()[0] == 1, '6 天前的赔率应保留（新策略 7 天）'


def test_cleanup_prematch_and_history_same_window(dao):
    """两张表用同一个时间窗口，不会出现一边有一边没有。"""
    now = _now()
    ko = now - timedelta(days=3)

    _insert_match(dao, PREFIX + 'same', ko)

    with dao.conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM kessbet_odds_prematch WHERE match_id=%s",
                  (PREFIX + 'same',))
        pre_before = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM kessbet_odds_history WHERE match_id=%s",
                  (PREFIX + 'same',))
        hist_before = c.fetchone()[0]

    dao.cleanup()

    with dao.conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM kessbet_odds_prematch WHERE match_id=%s",
                  (PREFIX + 'same',))
        pre_after = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM kessbet_odds_history WHERE match_id=%s",
                  (PREFIX + 'same',))
        hist_after = c.fetchone()[0]

    assert pre_after == pre_before, '3 天前的 prematch 应保留'
    assert hist_after == hist_before, '3 天前的 history 应保留'


# ---------- 平台移除的比赛 ----------

def test_mark_removed_matches(dao):
    """平台不再返回的比赛应被标记，而不是删除。

    实测：4 场比赛从平台赛程消失（延期/取消），接口返回 0 盘口。
    如果不标记，覆盖率检查会误判为"漏采"。
    """
    now = _now()
    keep_id = PREFIX + 'keep'
    gone_id = PREFIX + 'gone'
    _insert_match(dao, keep_id, now + timedelta(hours=5))
    _insert_match(dao, gone_id, now + timedelta(hours=5))

    # 只把 keep_id 视为"平台仍返回的"
    n = dao.mark_removed_matches('kessbet', 'sr:sport:1', [keep_id])
    assert n == 1, '应标记 1 场消失的比赛'

    with dao.conn.cursor() as c:
        c.execute("SELECT is_active FROM kessbet_match WHERE match_id=%s", (gone_id,))
        assert c.fetchone()[0] == 0, '消失的比赛应标 is_active=0'
        c.execute("SELECT is_active FROM kessbet_match WHERE match_id=%s", (keep_id,))
        assert c.fetchone()[0] == 1, '仍在列表里的比赛保持 active'


def test_reactivate_returning_match(dao):
    """比赛重新出现时应恢复 active。"""
    now = _now()
    mid = PREFIX + 'return'
    _insert_match(dao, mid, now + timedelta(hours=5))

    dao.mark_removed_matches('kessbet', 'sr:sport:1', [])
    with dao.conn.cursor() as c:
        c.execute("SELECT is_active FROM kessbet_match WHERE match_id=%s", (mid,))
        assert c.fetchone()[0] == 0

    # 重新出现在列表里
    _insert_match(dao, mid, now + timedelta(hours=5))
    dao.upsert_matches([type('M', (), {
        'platform': 'kessbet', 'match_id': mid, 'sport': 'sr:sport:1',
        'sportradar_id': '', 'league': '', 'country': '',
        'home_team': 'A', 'away_team': 'B',
        'kickoff_utc': now + timedelta(hours=5),
    })()])

    with dao.conn.cursor() as c:
        c.execute("SELECT is_active FROM kessbet_match WHERE match_id=%s", (mid,))
        assert c.fetchone()[0] == 1, '复活的比赛应恢复 active'
