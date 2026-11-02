"""验证对账逻辑：比例保护 + 增改删三种操作。

用 SQLite 模拟 MySQL 行为（UPSERT 语法不同，逻辑相同）。
真实数据来自 KessBet 接口的快照。

运行: py test/scratch/reconcile_test.py
"""
import sqlite3
import sys
import os
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))


class Sel(NamedTuple):
    market: str
    side: str
    line: str
    label: str
    odds: float

    @property
    def key(self):
        return (self.market, self.side, self.line, self.label)


SCHEMA = """
CREATE TABLE odds (
    match_id  TEXT NOT NULL,
    market    TEXT NOT NULL,
    side      TEXT NOT NULL DEFAULT '',
    line      TEXT NOT NULL DEFAULT '',
    selection TEXT NOT NULL,
    odds      REAL NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    removed_by TEXT DEFAULT NULL,
    PRIMARY KEY (match_id, market, side, line, selection)
);
"""


def connect():
    conn = sqlite3.connect(':memory:')
    conn.executescript(SCHEMA)
    return conn


def upsert(conn, match_id, sels):
    """只增改，不删。"""
    sql = """
    INSERT INTO odds (match_id, market, side, line, selection, odds, is_active)
    VALUES (?,?,?,?,?,?,1)
    ON CONFLICT(match_id, market, side, line, selection) DO UPDATE SET
        odds = excluded.odds,
        is_active = 1,
        removed_by = NULL
    """
    conn.executemany(sql, [(match_id, s.market, s.side, s.line, s.label, s.odds)
                           for s in sels])


def replace_match(conn, match_id, api_sels):
    """增改 + 把接口里没有的标记失效。"""
    upsert(conn, match_id, api_sels)
    api_keys = {s.key for s in api_sels}
    rows = conn.execute(
        "SELECT market, side, line, selection FROM odds "
        "WHERE match_id=? AND is_active=1", (match_id,)).fetchall()
    stale = [r for r in rows if r not in api_keys]
    if stale:
        conn.executemany(
            "UPDATE odds SET is_active=0, removed_by='reconcile' "
            "WHERE match_id=? AND market=? AND side=? AND line=? AND selection=?",
            [(match_id, *r) for r in stale])
    return len(stale)


def get_active(conn, match_id):
    rows = conn.execute(
        "SELECT market, side, line, selection, odds FROM odds "
        "WHERE match_id=? AND is_active=1", (match_id,)).fetchall()
    return {r[:4]: r[4] for r in rows}


def is_ratio_suspicious(api_n, db_n):
    """接口数量远少于库里的，判定为残缺响应。"""
    if db_n == 0:
        return False
    if api_n == 0:
        return True
    return api_n < db_n * 0.5


def reconcile_one(conn, match_id, api_sels):
    """对账单场。返回 (动作, 有效行数)。"""
    db = get_active(conn, match_id)

    if not db:
        upsert(conn, match_id, api_sels)
        return 'init', len(api_sels)

    if is_ratio_suspicious(len(api_sels), len(db)):
        upsert(conn, match_id, api_sels)
        return 'skip_removal', len(api_sels)

    removed = replace_match(conn, match_id, api_sels)
    return f'replace(-{removed})', len(api_sels)


# ---------------- 测试 ----------------

def test_normal_reconcile():
    """正常：接口和库一致，无操作。"""
    conn = connect()
    sels = [Sel('Total', '', '2.5', 'over', 1.85),
            Sel('Total', '', '2.5', 'under', 2.01),
            Sel('1x2', '', '', '1', 1.47)]
    upsert(conn, 'm1', sels)
    action, n = reconcile_one(conn, 'm1', sels)
    assert get_active(conn, 'm1') == {s.key: s.odds for s in sels}
    print(f'  OK 正常对账: {action}, {n} 行')


def test_reconcile_adds_missing():
    """库里少了一行（推送丢帧），对账补上。"""
    conn = connect()
    old = [Sel('Total', '', '2.5', 'over', 1.85)]
    upsert(conn, 'm1', old)

    fresh = old + [Sel('Total', '', '2.5', 'under', 2.01)]
    action, n = reconcile_one(conn, 'm1', fresh)
    active = get_active(conn, 'm1')
    assert len(active) == 2, f'应补到 2 行，实际 {len(active)}'
    assert active[('Total', '', '2.5', 'under')] == 2.01
    print(f'  OK 补漏: {action}, {len(active)} 行')


def test_reconcile_removes_stale():
    """库里有、接口没有 → 标记失效（推送没给 delete 的撤盘）。"""
    conn = connect()
    old = [Sel('Total', '', '2.5', 'over', 1.85),
           Sel('Total', '', '2.5', 'under', 2.01),
           Sel('1x2', '', '', '1', 1.47)]
    upsert(conn, 'm1', old)

    fresh = [Sel('Total', '', '2.5', 'over', 1.90),
             Sel('1x2', '', '', '1', 1.47)]
    action, n = reconcile_one(conn, 'm1', fresh)
    active = get_active(conn, 'm1')
    assert len(active) == 2, f'应剩 2 行，实际 {len(active)}'
    assert ('Total', '', '2.5', 'under') not in active
    # 失效行仍在表里
    total_rows = conn.execute("SELECT COUNT(*) FROM odds WHERE match_id='m1'").fetchone()[0]
    assert total_rows == 3, '失效行应保留（软删除）'
    print(f'  OK 清理撤盘: {action}, 激活 {len(active)} 行, 表内共 {total_rows} 行')


def test_ratio_protection():
    """接口返回残缺（3 个 vs 库里 400 个）→ 不删，只增改。"""
    conn = connect()
    old = [Sel('Total', '', f'{i}.5', 'over', 1.5 + i * 0.1) for i in range(400)]
    upsert(conn, 'm1', old)

    # 接口只返回 3 个
    partial = old[:3]
    action, n = reconcile_one(conn, 'm1', partial)
    active = get_active(conn, 'm1')
    assert len(active) == 400, f'残缺响应不应删数据，实际剩 {len(active)}'
    assert action == 'skip_removal'
    print(f'  OK 比例保护: {action}, 保留 {len(active)} 行')


def test_ratio_allows_legit_shrink():
    """接口返回 60% → 正常清理（真的撤了很多盘）。"""
    conn = connect()
    old = [Sel('Total', '', f'{i}.5', 'over', 1.5 + i * 0.1) for i in range(100)]
    upsert(conn, 'm1', old)

    fresh = old[:60]
    action, n = reconcile_one(conn, 'm1', fresh)
    active = get_active(conn, 'm1')
    assert len(active) == 60, f'应清理到 60 行，实际 {len(active)}'
    print(f'  OK 合法缩减: {action}, {len(active)} 行')


def test_revive_removed_market():
    """之前撤掉的盘口重新出现 → 复活。"""
    conn = connect()
    sels = [Sel('Total', '', '2.5', 'over', 1.85),
            Sel('Total', '', '2.5', 'under', 2.01)]
    upsert(conn, 'm1', sels)

    # 撤掉 under
    replace_match(conn, 'm1', sels[:1])
    assert len(get_active(conn, 'm1')) == 1

    # 重新挂上
    upsert(conn, 'm1', sels)
    active = get_active(conn, 'm1')
    assert len(active) == 2, f'复活的盘口应恢复，实际 {len(active)}'
    print(f'  OK 复活: {len(active)} 行')


def test_empty_api_does_not_wipe():
    """接口返回空 → 绝不清空。"""
    conn = connect()
    old = [Sel('Total', '', '2.5', 'over', 1.85)]
    upsert(conn, 'm1', old)

    action, n = reconcile_one(conn, 'm1', [])
    active = get_active(conn, 'm1')
    assert len(active) == 1, '空响应绝不删数据'
    print(f'  OK 空响应保护: {action}, 保留 {len(active)} 行')


def test_first_init():
    """库里没有 → 初始化。"""
    conn = connect()
    sels = [Sel('Total', '', '2.5', 'over', 1.85)]
    action, n = reconcile_one(conn, 'm1', sels)
    assert action == 'init'
    assert len(get_active(conn, 'm1')) == 1
    print(f'  OK 初始化: {action}')


if __name__ == '__main__':
    print('对账逻辑测试\n')
    tests = [
        test_first_init,
        test_normal_reconcile,
        test_reconcile_adds_missing,
        test_reconcile_removes_stale,
        test_ratio_protection,
        test_ratio_allows_legit_shrink,
        test_revive_removed_market,
        test_empty_api_does_not_wipe,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f'  FAIL {t.__name__}: {e}')
    print(f'\n{len(tests) - failed}/{len(tests)} 通过')
    sys.exit(1 if failed else 0)
