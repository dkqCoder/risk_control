"""collect/writer.py 的测试。用真实 MySQL，测完清理。

运行: py -m pytest test/test_writer.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from common.models import Match, Selection
from db.dao import Dao
from collect.writer import replace_match

TEST_MATCH_ID = '__TEST_WRITER__'


def _match(kickoff_offset_hours=24):
    return Match(
        platform='kessbet',
        match_id=TEST_MATCH_ID,
        sport='sr:sport:1',
        league='Test League',
        country='Test',
        home_team='Home',
        away_team='Away',
        kickoff_time=datetime.now(timezone.utc) + timedelta(hours=kickoff_offset_hours),
        sportradar_id='sr:match:test',
    )


def _sels(*specs):
    return [Selection(m, s, l, lab, o) for m, s, l, lab, o in specs]


@pytest.fixture
def dao():
    d = Dao()
    _purge(d)
    yield d
    _purge(d)
    d.close()


def _purge(d):
    """清掉本文件造的假数据。

    必须带 platform 条件：表上的索引是 (platform, match_id, ...) 和
    (platform, match_id, changed_time)，只按 match_id 过滤用不上最左前缀，
    会退化成全表扫描。kessbet_odds_history 有近 400 万行，实测一次
    3.2s vs 0.1s，12 个用例前后各清一次就是几十秒。
    """
    with d.conn.cursor() as c:
        for t in ('kessbet_odds_prematch', 'kessbet_odds_history', 'kessbet_match'):
            c.execute(f"DELETE FROM {t} WHERE platform='kessbet' AND match_id=%s",
                      (TEST_MATCH_ID,))
    d.conn.commit()


def test_write_new_match(dao):
    m = _match()
    sels = _sels(
        ('1x2', '', '', '1', 1.47),
        ('1x2', '', '', 'X', 4.80),
        ('Total', '', '2.5', 'over', 1.85),
    )
    r = replace_match(dao, m, sels)
    assert r['written'] == 3
    assert dao.count_odds(TEST_MATCH_ID) == 3


def test_replace_removes_stale(dao):
    m = _match()
    initial = _sels(
        ('1x2', '', '', '1', 1.47),
        ('1x2', '', '', 'X', 4.80),
        ('Total', '', '2.5', 'over', 1.85),
    )
    replace_match(dao, m, initial)

    # 第二次少了 Total 那条
    fresh = _sels(
        ('1x2', '', '', '1', 1.50),
        ('1x2', '', '', 'X', 4.60),
    )
    replace_match(dao, m, fresh)

    assert dao.count_odds(TEST_MATCH_ID) == 2
    keys = dao.fetch_keys(TEST_MATCH_ID)
    assert ('Total', '', '2.5', 'over') not in keys


def test_prev_odds_keeps_previous_value(dao):
    m = _match()
    replace_match(dao, m, _sels(('Total', '', '2.5', 'over', 1.85)))
    replace_match(dao, m, _sels(('Total', '', '2.5', 'over', 1.90)))

    with dao.conn.cursor() as c:
        c.execute("SELECT odds, prev_odds FROM kessbet_odds_prematch "
                  "WHERE match_id=%s AND market='Total'", (TEST_MATCH_ID,))
        odds, prev = c.fetchone()
    assert float(odds) == 1.90
    assert float(prev) == 1.85, 'prev_odds 应保存上一轮的赔率'


def test_skip_after_kickoff(dao):
    """已开赛的比赛不写入。"""
    m = _match(kickoff_offset_hours=-1)
    r = replace_match(dao, m, _sels(('1x2', '', '', '1', 1.47)))
    assert r['written'] == 0
    assert dao.count_odds(TEST_MATCH_ID) == 0


def test_side_markets_do_not_collide(dao):
    """side='H' 和 side='' 的同名盘口必须能共存。"""
    m = _match()
    sels = _sels(
        ('Total', '', '1.5', 'over', 1.28),
        ('Total', 'H', '1.5', 'over', 1.46),
        ('Total', 'A', '1.5', 'over', 2.30),
    )
    replace_match(dao, m, sels)
    assert dao.count_odds(TEST_MATCH_ID) == 3
    keys = dao.fetch_keys(TEST_MATCH_ID)
    assert ('Total', '', '1.5', 'over') in keys
    assert ('Total', 'H', '1.5', 'over') in keys
    assert ('Total', 'A', '1.5', 'over') in keys


# ---------- 历史表 ----------

def _history(dao):
    with dao.conn.cursor() as c:
        c.execute("""SELECT market, side, line, selection, odds, prev_odds
                     FROM kessbet_odds_history WHERE match_id=%s
                     ORDER BY id""", (TEST_MATCH_ID,))
        return [(r[0], r[1], r[2], r[3], float(r[4]),
                 float(r[5]) if r[5] is not None else None) for r in c.fetchall()]


def test_history_records_first_appearance(dao):
    """首次出现也写历史，prev_odds 为 NULL。"""
    m = _match()
    r = replace_match(dao, m, _sels(('1x2', '', '', '1', 1.47)))
    assert r['changed'] == 1
    hist = _history(dao)
    assert hist == [('1x2', '', '', '1', 1.47, None)]


def test_history_only_records_changes(dao):
    """没变化的选项不写历史。"""
    m = _match()
    sels = _sels(('1x2', '', '', '1', 1.47), ('1x2', '', '', 'X', 4.80))
    replace_match(dao, m, sels)          # 首次：2 条
    assert len(_history(dao)) == 2

    # 第二次：只有 '1' 变了
    replace_match(dao, m, _sels(('1x2', '', '', '1', 1.50),
                                ('1x2', '', '', 'X', 4.80)))
    hist = _history(dao)
    assert len(hist) == 3, '只应新增 1 条'
    assert hist[-1] == ('1x2', '', '', '1', 1.50, 1.47)


def test_history_no_change_no_write(dao):
    """完全相同的赔率不产生历史记录。"""
    m = _match()
    sels = _sels(('Total', '', '2.5', 'over', 1.85))
    replace_match(dao, m, sels)
    replace_match(dao, m, sels)
    replace_match(dao, m, sels)
    assert len(_history(dao)) == 1, '重复写入相同赔率不应产生历史'


def test_history_accumulates_multiple_changes(dao):
    """同一选项多次变化累积多条记录。"""
    m = _match()
    for odds in (1.85, 1.90, 1.80, 1.95):
        replace_match(dao, m, _sels(('Total', '', '2.5', 'over', odds)))

    hist = _history(dao)
    assert len(hist) == 4
    assert [h[4] for h in hist] == [1.85, 1.90, 1.80, 1.95]
    assert [h[5] for h in hist] == [None, 1.85, 1.90, 1.80]


def test_history_not_written_after_kickoff(dao):
    """已开赛的比赛不写历史。"""
    m = _match(kickoff_offset_hours=-1)
    replace_match(dao, m, _sels(('1x2', '', '', '1', 1.47)))
    assert _history(dao) == []


# ---------- 字段超长保护 ----------

def test_overlong_market_is_truncated(dao):
    """超长盘口名截断而非整场失败。"""
    m = _match()
    long_market = 'X' * 100
    r = replace_match(dao, m, _sels((long_market, '', '', 'yes', 1.5)))
    assert r['written'] == 1
    assert dao.count_odds(TEST_MATCH_ID) == 1

    with dao.conn.cursor() as c:
        c.execute("SELECT LENGTH(market), market FROM kessbet_odds_prematch "
                  "WHERE match_id=%s", (TEST_MATCH_ID,))
        length, stored = c.fetchone()
    assert length == 64
    assert stored == 'X' * 64


def test_truncated_key_matches_on_replace(dao):
    """截断后 upsert 和 delete 的 key 必须一致，否则会误删自己刚写的行。"""
    m = _match()
    long_market = 'Y' * 100
    sels = _sels((long_market, '', '', 'yes', 1.5))
    replace_match(dao, m, sels)
    # 再写一次相同数据：如果 key 不一致，会被当成 stale 删掉
    replace_match(dao, m, sels)
    assert dao.count_odds(TEST_MATCH_ID) == 1, '不应被误删'
