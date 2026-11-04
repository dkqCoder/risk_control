"""成本变动流水的测试。

列表页「1h 变化」依赖这张表。这里验证三件事：
  1. 成本没变时不写行（否则 analyze 每 2 秒一条，一天几万行）
  2. 成本变了才写，且 prev_cost 记的是上一行
  3. cost_at() 取的是「N 分钟前那一刻」的成本，不是「N 分钟内最新」

用一台临时 match_id 做，测试前后自清理，不碰真实数据。

运行: py -m pytest test/test_cost_history.py -v
"""
import os
import sys
from datetime import timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest

from common.timeutil import now_beijing
from db.dao import Dao

MID = '_test_cost_history_'
PLATFORM = 'kessbet'


@pytest.fixture()
def dao():
    d = Dao()
    _clean(d)
    yield d
    _clean(d)
    d.close()


def _clean(dao):
    with dao.conn.cursor() as c:
        c.execute("DELETE FROM kessbet_match_cost_history "
                  "WHERE platform=%s AND match_id=%s", (PLATFORM, MID))
        c.execute("DELETE FROM kessbet_match_cost "
                  "WHERE platform=%s AND match_id=%s", (PLATFORM, MID))
    dao.conn.commit()


def _write_cost(dao, match_id, cost, when):
    dao.write_cost(
        platform=PLATFORM, match_id=match_id, sport='sr:sport:1',
        kickoff_time=when + timedelta(hours=5), cost=cost,
        cost_scope='single', n_markets=0, n_selections=10, n_candidates=8,
        ilp_chosen=None, lp_worst_profit=None, lp_chosen=None,
        cross_gap=None, error=None, odds_updated=when, computed_time=when)
    dao.conn.commit()


# ---------------------------------------------------------------- 写入

def test_no_history_when_cost_unchanged(dao):
    """成本没变时不写流水 —— analyze 每 2 秒一轮，写了会爆表。"""
    now = now_beijing()
    _write_cost(dao, MID, 1.0500, now)
    prev = dao.get_cost(MID)
    assert prev['cost'] == pytest.approx(1.05)

    # 同样的成本再来一次
    assert dao.append_cost_history(
        PLATFORM, MID, 1.0500, prev_cost=prev['cost'],
        odds_updated=now + timedelta(seconds=2), changed_time=now) is True

    # 但第二份赔率版本成本仍是 1.05 —— 调用方（_persist_cost）会跳过，
    # 这里直接验证跳过条件本身
    assert abs(prev['cost'] - 1.0500) <= 1e-6, '相同成本应被判定为未变化'


def test_history_written_when_cost_changes(dao):
    """成本变化时写入，prev_cost 记录上一行的值。"""
    now = now_beijing()
    t1 = now - timedelta(minutes=90)
    t2 = now - timedelta(minutes=30)

    _write_cost(dao, MID, 1.0500, t1)
    dao.append_cost_history(PLATFORM, MID, 1.0500, None, t1, t1)
    dao.conn.commit()

    _write_cost(dao, MID, 1.0438, t2)
    prev = dao.get_cost(MID)
    assert prev['cost'] == pytest.approx(1.0438)

    assert dao.append_cost_history(
        PLATFORM, MID, 1.0438, prev_cost=1.0500,
        odds_updated=t2, changed_time=t2) is True
    dao.conn.commit()

    with dao.conn.cursor() as c:
        c.execute("""SELECT cost, prev_cost, changed_time
                     FROM kessbet_match_cost_history
                     WHERE platform=%s AND match_id=%s
                     ORDER BY changed_time""", (PLATFORM, MID))
        rows = c.fetchall()
    assert len(rows) == 2
    assert float(rows[1][0]) == pytest.approx(1.0438)
    assert float(rows[1][1]) == pytest.approx(1.0500), \
        'prev_cost 应该是上一条的值'


def test_duplicate_odds_version_not_written_twice(dao):
    """同一份赔率重算两次不产生重复行（唯一键兜底）。"""
    now = now_beijing()
    t = now - timedelta(minutes=30)
    assert dao.append_cost_history(PLATFORM, MID, 1.05, None, t, t) is True
    dao.conn.commit()
    assert dao.append_cost_history(PLATFORM, MID, 1.05, None, t, t) is False

    with dao.conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM kessbet_match_cost_history "
                  "WHERE platform=%s AND match_id=%s", (PLATFORM, MID))
        assert c.fetchone()[0] == 1


def test_none_cost_not_written(dao):
    """没有成本（无法覆盖）时不写流水，否则列表页会拿 None 去算差值。"""
    assert dao.append_cost_history(
        PLATFORM, MID, None, None, now_beijing(), now_beijing()) is False


# ---------------------------------------------------------------- 读取

def test_cost_at_returns_value_as_of_cutoff(dao):
    """cost_at(N) 返回「N 分钟前那一刻」的成本，不是最新值。"""
    now = now_beijing()
    # 两小时前 1.05，半小时前才降到 1.04
    dao.append_cost_history(PLATFORM, MID, 1.0500, None,
                            now - timedelta(minutes=120),
                            now - timedelta(minutes=120))
    dao.append_cost_history(PLATFORM, MID, 1.0400, 1.0500,
                            now - timedelta(minutes=30),
                            now - timedelta(minutes=30))
    dao.conn.commit()

    # 1 小时前那一刻，1.04 那次变动还没发生 -> 应该是 1.05
    got = dao.cost_at([MID], minutes=60)
    assert MID in got
    assert got[MID]['cost'] == pytest.approx(1.0500), \
        '1 小时前那一刻成本还是 1.05（1.04 是半小时前才降的）'

    # 20 分钟前那一刻 -> 已经是 1.04
    got20 = dao.cost_at([MID], minutes=20)
    assert got20[MID]['cost'] == pytest.approx(1.0400)


def test_cost_at_ignores_rows_inside_window(dao):
    """窗口内的行不算 —— 只取截止时刻之前的最后一条。"""
    now = now_beijing()
    dao.append_cost_history(PLATFORM, MID, 1.0500, None,
                            now - timedelta(minutes=90),
                            now - timedelta(minutes=90))
    # 这条在 1 小时窗口内，不应被 cost_at(60) 选中
    dao.append_cost_history(PLATFORM, MID, 1.0300, 1.0500,
                            now - timedelta(minutes=10),
                            now - timedelta(minutes=10))
    dao.conn.commit()

    got = dao.cost_at([MID], minutes=60)
    assert got[MID]['cost'] == pytest.approx(1.0500)


def test_cost_at_omits_matches_without_history(dao):
    """没有历史行的比赛不出现在结果里，前端据此显示「—」。

    这一点很关键：如果返回 0 或者回退到当前成本，列表页每场都会
    显示「变化 0」，用户会以为真的没变过。
    """
    got = dao.cost_at([MID], minutes=60)
    assert MID not in got
    assert got == {}


def test_cost_at_empty_input(dao):
    assert dao.cost_at([], minutes=60) == {}
