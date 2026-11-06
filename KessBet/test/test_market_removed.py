"""撤盘日志（kessbet_market_removed）的测试。

这张表记录「某个选项从平台上消失了」这件事 —— 因为 prematch 是全量替换、
物理删除，删完就查不到了。

三个关键点：
  1. 必须记录被撤时的**最后赔率**（删完就没了，事后没法补）
  2. 写日志失败不能让整场刷新失败（观察用，不该影响主流程）
  3. 同一个选项撤了又回来又撤，要能记多条

运行: py -m pytest test/test_market_removed.py -v
"""
import os
import sys
from datetime import timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest

from collect.writer import replace_match
from common.models import Match, Selection
from common.timeutil import now_beijing
from db.dao import Dao

MID = '_test_removed_'
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
        for t in ('kessbet_odds_prematch', 'kessbet_market_removed'):
            c.execute(f"DELETE FROM {t} WHERE platform=%s AND match_id=%s",
                      (PLATFORM, MID))
    d.conn.commit()


def _match(when=None):
    return Match(platform=PLATFORM, match_id=MID, sport='sr:sport:1',
                 league='Test', country='Test', home_team='A', away_team='B',
                 kickoff_time=when or (now_beijing() + timedelta(days=1)))


def _sels(*items):
    return [Selection(market=m, side=s, line=l, label=lab, odds=o)
            for m, s, l, lab, o in items]


def _removed(dao):
    with dao.conn.cursor() as c:
        c.execute("""SELECT market, side, line, selection, odds, source
                     FROM kessbet_market_removed
                     WHERE platform=%s AND match_id=%s
                     ORDER BY removed_time, market, selection""",
                  (PLATFORM, MID))
        return c.fetchall()


# ---------------------------------------------------------------- 基本行为

def test_removal_is_logged(dao):
    """撤掉的选项要进日志表，且带上被撤时的赔率。"""
    m = _match()
    now = now_beijing()
    replace_match(dao, m, _sels(
        ('1x2', '', '', '1', 1.47),
        ('Total', '', '2.5', 'over', 1.85),
    ), now)
    assert len(_removed(dao)) == 0, '首次写入没有撤盘，日志应为空'

    # 第二次少了 Total over
    replace_match(dao, m, _sels(
        ('1x2', '', '', '1', 1.50),
    ), now)

    rows = _removed(dao)
    assert len(rows) == 1
    market, side, line, sel, odds, source = rows[0]
    assert (market, side, line, sel) == ('Total', '', '2.5', 'over')
    assert float(odds) == pytest.approx(1.85), \
        '记录的应该是被撤时的最后赔率，不是别的值'
    assert source == 'http'


def test_removal_still_deletes_row(dao):
    """记日志不能影响物理删除 —— prematch 里该没的还是得没。"""
    m = _match()
    now = now_beijing()
    replace_match(dao, m, _sels(
        ('1x2', '', '', '1', 1.47),
        ('Total', '', '2.5', 'over', 1.85),
    ), now)
    replace_match(dao, m, _sels(('1x2', '', '', '1', 1.50)), now)

    keys = dao.fetch_keys(MID)
    assert ('Total', '', '2.5', 'over') not in keys, '撤掉的选项必须从 prematch 删除'
    assert dao.count_odds(MID) == 1


def test_multiple_removals_logged(dao):
    """一次撤掉多个选项，每个都记一条。"""
    m = _match()
    now = now_beijing()
    replace_match(dao, m, _sels(
        ('1x2', '', '', '1', 1.47),
        ('1x2', '', '', 'X', 4.80),
        ('Total', '', '2.5', 'over', 1.85),
    ), now)
    replace_match(dao, m, _sels(('1x2', '', '', '1', 1.50)), now)

    assert len(_removed(dao)) == 2


def test_reappear_and_remove_again(dao):
    """撤了又回来又撤 —— 应该记两条，保留完整历史。

    这是独立表相对于「prematch 加 removed_time 字段」的优势：
    加字段只能记最后一次。
    """
    m = _match()
    base = now_beijing()

    sels = _sels(('1x2', '', '', '1', 1.47), ('Total', '', '2.5', 'over', 1.85))
    replace_match(dao, m, sels, base)                       # 两个都在
    replace_match(dao, m, _sels(('1x2', '', '', '1', 1.50)),
                  base + timedelta(seconds=10))              # Total 消失 -> 第 1 条
    replace_match(dao, m, sels, base + timedelta(seconds=20))  # Total 回来
    replace_match(dao, m, _sels(('1x2', '', '', '1', 1.50)),
                  base + timedelta(seconds=30))              # 又消失 -> 第 2 条

    rows = _removed(dao)
    assert len(rows) == 2, f'应记 2 条，实际 {len(rows)}'
    assert all(r[3] == 'over' for r in rows)


def test_odds_recorded_is_the_last_one(dao):
    """被撤时的赔率应该是库里当时的值，不是更早的。"""
    m = _match()
    base = now_beijing()
    replace_match(dao, m, _sels(
        ('Total', '', '2.5', 'over', 1.85),
        ('1x2', '', '', '1', 1.47),
    ), base)
    # 赔率先变化
    replace_match(dao, m, _sels(
        ('Total', '', '2.5', 'over', 1.70),
        ('1x2', '', '', '1', 1.47),
    ), base + timedelta(seconds=5))
    # 然后被撤
    replace_match(dao, m, _sels(('1x2', '', '', '1', 1.47)),
                  base + timedelta(seconds=10))

    rows = _removed(dao)
    assert len(rows) == 1
    assert float(rows[0][4]) == pytest.approx(1.70), \
        '应该是撤盘前的最后赔率 1.70，不是最初的 1.85'


def test_no_removal_when_nothing_disappears(dao):
    """选项没少就不该写日志。"""
    m = _match()
    now = now_beijing()
    sels = _sels(('1x2', '', '', '1', 1.47), ('Total', '', '2.5', 'over', 1.85))
    replace_match(dao, m, sels, now)
    replace_match(dao, m, _sels(
        ('1x2', '', '', '1', 1.50),
        ('Total', '', '2.5', 'over', 1.90),
    ), now)
    assert len(_removed(dao)) == 0


# ---------------------------------------------------------------- 容错

def test_log_failure_does_not_block_deletion(dao, monkeypatch):
    """写日志失败不能让整场刷新失败 —— 赔率更新比日志重要。"""
    m = _match()
    now = now_beijing()
    replace_match(dao, m, _sels(
        ('1x2', '', '', '1', 1.47),
        ('Total', '', '2.5', 'over', 1.85),
    ), now)

    def boom(*a, **kw):
        raise RuntimeError('模拟日志表不可用')

    monkeypatch.setattr(dao, 'log_removed', boom)

    # 不应该抛异常
    result = replace_match(dao, m, _sels(('1x2', '', '', '1', 1.50)), now)
    assert result['written'] == 1
    # 删除照常发生
    assert dao.count_odds(MID) == 1
