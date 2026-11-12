"""滚球数据 DAO 的测试。

三张表：
  kessbet_match_live        比赛实时状态（一场一行，覆盖）
  kessbet_odds_live         滚球赔率当前值
  kessbet_odds_live_history 赔率变动流水（带比分上下文）

运行: py -m pytest test/test_live_dao.py -v
"""
import os
import sys
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest

from common.timeutil import now_beijing
from db.dao import Dao

MID = '_test_live_'
PLATFORM = 'kessbet'


@pytest.fixture()
def dao():
    d = Dao()
    _purge(d)
    yield d
    _purge(d)
    d.close()


def _purge(d):
    with d.conn.cursor() as c:
        for t in ('kessbet_odds_live', 'kessbet_odds_live_history',
                  'kessbet_match_live',
                  'kessbet_market_removed', 'kessbet_match'):
            c.execute(f"DELETE FROM {t} WHERE platform=%s AND match_id=%s",
                      (PLATFORM, MID))
    d.conn.commit()


def _insert_match(dao, kickoff):
    with dao.conn.cursor() as c:
        c.execute("""INSERT INTO kessbet_match
                     (platform, match_id, sport, league, country,
                      home_team, away_team, kickoff_time,
                      first_seen_time, updated_time)
                     VALUES (%s,%s,'sr:sport:1','L','C','H','A',%s,NOW(),NOW())""",
                  (PLATFORM, MID, kickoff))
    dao.conn.commit()


def _state(ms=7, mt='65:54', hmp='1', amp='0',
           hck=5, ack=3, hyc=2, ayc=1, hrc=0, arc=0):
    return {'ms': ms, 'mt': mt, 'hmp': hmp, 'amp': amp,
            'hck': hck, 'ack': ack, 'hyc': hyc, 'ayc': ayc,
            'hrc': hrc, 'arc': arc}


def _odds(*items):
    return [(m, s, l, lab, o) for m, s, l, lab, o in items]


# ---------------------------------------------------------------- live_matches

def test_live_matches_returns_in_window_only(dao):
    """只返回「已开赛且在窗口内」的比赛。"""
    now = now_beijing()
    _insert_match(dao, now - timedelta(minutes=30))    # 进行中
    rows = dao.live_matches(window_min=150)
    ids = [r['match_id'] for r in rows]
    assert MID in ids, '进行中的比赛应返回'

    # 改成未来开赛 -> 不该返回
    _purge(dao)
    _insert_match(dao, now + timedelta(hours=2))
    assert MID not in [r['match_id'] for r in dao.live_matches(150)]


def test_live_matches_excludes_old(dao):
    """超过窗口的不返回。"""
    now = now_beijing()
    _insert_match(dao, now - timedelta(minutes=200))
    assert MID not in [r['match_id'] for r in dao.live_matches(150)]


# ---------------------------------------------------------------- save_live_state

def test_save_state_writes_all_fields(dao):
    now = now_beijing()
    kickoff = now - timedelta(minutes=65)
    dao.save_live_state(MID, _state(), kickoff)

    with dao.conn.cursor() as c:
        c.execute("""SELECT status, minute, home_score, away_score,
                            home_corner, away_corner,
                            home_yellow, away_yellow, home_red, away_red
                     FROM kessbet_match_live
                     WHERE platform=%s AND match_id=%s""", (PLATFORM, MID))
        r = c.fetchone()
    assert r is not None
    assert r[0] == 7            # status
    assert r[1] == 65           # minute（'65:54' 解析出 65）
    assert r[2] == 1 and r[3] == 0    # 比分
    assert r[4] == 5 and r[5] == 3    # 角球
    assert r[6] == 2 and r[7] == 1    # 黄牌
    assert r[8] == 0 and r[9] == 0    # 红牌


def test_save_state_overwrites_not_duplicates(dao):
    """一场一行，重复采是覆盖。"""
    now = now_beijing()
    dao.save_live_state(MID, _state(hmp='0', mt='10:00'), now)
    dao.save_live_state(MID, _state(hmp='2', mt='70:00'), now)

    with dao.conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM kessbet_match_live "
                  "WHERE platform=%s AND match_id=%s", (PLATFORM, MID))
        assert c.fetchone()[0] == 1
        c.execute("""SELECT home_score, minute FROM kessbet_match_live
                     WHERE platform=%s AND match_id=%s""", (PLATFORM, MID))
        r = c.fetchone()
    assert r[0] == 2 and r[1] == 70, '应该是最后写入的值'


def test_save_state_handles_empty_fields(dao):
    """未开赛的比赛 hmp/amp/mt 都是空字符串，不能炸。"""
    now = now_beijing()
    dao.save_live_state(MID, _state(ms=0, mt='', hmp='', amp='',
                                    hck=None, ack=None), now)
    with dao.conn.cursor() as c:
        c.execute("""SELECT status, minute, home_score, home_corner
                     FROM kessbet_match_live
                     WHERE platform=%s AND match_id=%s""", (PLATFORM, MID))
        r = c.fetchone()
    assert r[0] == 0
    assert r[1] is None      # 空 mt -> NULL
    assert r[2] is None      # 空比分 -> NULL
    assert r[3] is None


# ---------------------------------------------------------------- replace_live_odds

def test_replace_writes_odds(dao):
    now = now_beijing()
    kickoff = now - timedelta(minutes=30)
    res = dao.replace_live_odds(MID, kickoff,
                                _odds(('1x2', '', '', '1', 2.5),
                                      ('1x2', '', '', 'X', 3.2)),
                                minute=30, home_score=1, away_score=0)
    assert res['written'] == 2
    assert res['changed'] == 2      # 首次写入，两条都算变化

    with dao.conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM kessbet_odds_live "
                  "WHERE platform=%s AND match_id=%s", (PLATFORM, MID))
        assert c.fetchone()[0] == 2


def test_history_only_on_change(dao):
    """赔率没变不写流水。"""
    now = now_beijing()
    kickoff = now - timedelta(minutes=30)
    o = _odds(('1x2', '', '', '1', 2.5))

    dao.replace_live_odds(MID, kickoff, o, minute=30, home_score=0, away_score=0)
    res = dao.replace_live_odds(MID, kickoff, o, minute=31, home_score=0, away_score=0)
    assert res['changed'] == 0, '同样的赔率不该产生新流水'

    with dao.conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM kessbet_odds_live_history "
                  "WHERE platform=%s AND match_id=%s", (PLATFORM, MID))
        assert c.fetchone()[0] == 1


def test_history_carries_match_context(dao):
    """流水必须带比分和分钟 —— 这是滚球数据最核心的价值。"""
    now = now_beijing()
    kickoff = now - timedelta(minutes=60)
    dao.replace_live_odds(MID, kickoff,
                          _odds(('Total', '', '2.5', 'over', 3.9)),
                          minute=60, home_score=2, away_score=1)

    with dao.conn.cursor() as c:
        c.execute("""SELECT odds, prev_odds, minute, home_score, away_score
                     FROM kessbet_odds_live_history
                     WHERE platform=%s AND match_id=%s""", (PLATFORM, MID))
        r = c.fetchone()
    assert float(r[0]) == pytest.approx(3.9)
    assert r[1] is None          # 首次出现没有 prev
    assert r[2] == 60
    assert r[3] == 2 and r[4] == 1


def test_history_records_prev_odds_on_change(dao):
    """赔率变化时，prev_odds 记的是旧值。"""
    now = now_beijing()
    kickoff = now - timedelta(minutes=30)
    dao.replace_live_odds(MID, kickoff,
                          _odds(('1x2', '', '', '1', 2.5)),
                          minute=30, home_score=0, away_score=0)
    dao.replace_live_odds(MID, kickoff,
                          _odds(('1x2', '', '', '1', 2.2)),
                          minute=35, home_score=1, away_score=0)

    with dao.conn.cursor() as c:
        c.execute("""SELECT odds, prev_odds, minute, home_score
                     FROM kessbet_odds_live_history
                     WHERE platform=%s AND match_id=%s
                     ORDER BY id""", (PLATFORM, MID))
        rows = c.fetchall()
    assert len(rows) == 2
    assert float(rows[1][0]) == pytest.approx(2.2)
    assert float(rows[1][1]) == pytest.approx(2.5), 'prev_odds 应是旧值 2.5'
    assert rows[1][2] == 35, '比分变动的上下文也要记下来'
    assert rows[1][3] == 1


def test_prev_odds_updated_in_current_table(dao):
    """当前表的 prev_odds 也要跟着更新。"""
    now = now_beijing()
    kickoff = now - timedelta(minutes=30)
    dao.replace_live_odds(MID, kickoff, _odds(('1x2', '', '', '1', 2.5)), 30, 0, 0)
    dao.replace_live_odds(MID, kickoff, _odds(('1x2', '', '', '1', 2.2)), 35, 1, 0)

    with dao.conn.cursor() as c:
        c.execute("""SELECT odds, prev_odds FROM kessbet_odds_live
                     WHERE platform=%s AND match_id=%s""", (PLATFORM, MID))
        r = c.fetchone()
    assert float(r[0]) == pytest.approx(2.2)
    assert float(r[1]) == pytest.approx(2.5)


def test_empty_selections_is_noop(dao):
    res = dao.replace_live_odds(MID, now_beijing(), [], 30, 0, 0)
    assert res['written'] == 0 and res['changed'] == 0


# ---------------------------------------------------------------- mark_live_finished

def test_mark_finished_sets_status_and_clears_odds(dao):
    now = now_beijing()
    kickoff = now - timedelta(minutes=100)
    dao.save_live_state(MID, _state(), kickoff)
    dao.replace_live_odds(MID, kickoff, _odds(('1x2', '', '', '1', 2.5)), 100, 1, 1)

    dao.mark_live_finished(MID)

    with dao.conn.cursor() as c:
        c.execute("SELECT status FROM kessbet_match_live "
                  "WHERE platform=%s AND match_id=%s", (PLATFORM, MID))
        assert c.fetchone()[0] == 100
        c.execute("SELECT COUNT(*) FROM kessbet_odds_live "
                  "WHERE platform=%s AND match_id=%s", (PLATFORM, MID))
        assert c.fetchone()[0] == 0, '结束后滚球赔率应清掉'

    # 但流水要保留
    with dao.conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM kessbet_odds_live_history "
                  "WHERE platform=%s AND match_id=%s", (PLATFORM, MID))
        assert c.fetchone()[0] == 1, '历史流水不能删'


# ---------------------------------------------------------------- cleanup

def test_cleanup_removes_old_data(dao):
    """三张表按各自的 kickoff_time 独立清理。"""
    now = now_beijing()
    old = now - timedelta(days=5)
    dao.save_live_state(MID, _state(), old)
    dao.replace_live_odds(MID, old, _odds(('1x2', '', '', '1', 2.5)), 90, 0, 0)

    dao.cleanup_live(days=3)

    with dao.conn.cursor() as c:
        for t in ('kessbet_match_live', 'kessbet_odds_live',
                  'kessbet_odds_live_history'):
            c.execute(f"SELECT COUNT(*) FROM {t} "
                      f"WHERE platform=%s AND match_id=%s", (PLATFORM, MID))
            assert c.fetchone()[0] == 0, f'{t} 应被清理'


# ---------------------------------------------------------------- 撤盘

def test_replace_live_odds_deletes_stale(dao):
    """接口里没有的选项必须删掉。

    之前漏了这一步：滚球盘口大量上下架，已撤的行会一直留着。
    实测某场表内 353 行、平台只给 170 行，241 个是已撤的。
    """
    now = now_beijing()
    _insert_match(dao, now + timedelta(hours=1))

    dao.replace_live_odds(MID, now, [
        ('Total', '', '2.5', 'over', 1.8),
        ('Total', '', '2.5', 'under', 2.0),
        ('1x2', '', '', '1', 2.5),
    ], 10, 0, 0)

    # 第二次只给两个选项：1x2 那个必须被删
    r = dao.replace_live_odds(MID, now, [
        ('Total', '', '2.5', 'over', 1.8),
        ('Total', '', '2.5', 'under', 2.0),
    ], 11, 0, 0)

    assert r['removed'] == 1

    with dao.conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM kessbet_odds_live "
                  "WHERE platform=%s AND match_id=%s", (PLATFORM, MID))
        assert c.fetchone()[0] == 2
        c.execute("SELECT COUNT(*) FROM kessbet_market_removed "
                  "WHERE platform=%s AND match_id=%s", (PLATFORM, MID))
        assert c.fetchone()[0] == 1, '撤盘要留日志'


def test_replace_live_odds_no_delete_when_unchanged(dao):
    now = now_beijing()
    _insert_match(dao, now + timedelta(hours=1))
    sels = [('Total', '', '2.5', 'over', 1.8),
            ('Total', '', '2.5', 'under', 2.0)]
    dao.replace_live_odds(MID, now, sels, 10, 0, 0)
    r = dao.replace_live_odds(MID, now, sels, 11, 0, 0)
    assert r['removed'] == 0
    assert r['changed'] == 0


# ---------------------------------------------------------------- 窗口成本
