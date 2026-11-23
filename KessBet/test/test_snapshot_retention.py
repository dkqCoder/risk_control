"""live_snapshot 保留期必须与滚球数据对齐，且不能删掉证据引用。

背景（2026-11-23）：表从 5.7G 涨到 14.7G 只用了两天，磁盘被写满，
MySQL 无法写临时表、整个服务停摆。原保留期是 30 天 —— 对这张表的增速
（每天数千份、每份几十 KB 原始响应）太长了。

命中证据已经另存到 data/evidence/ 和 kessbet_arbitrage_snapshot，
原始输入快照不需要留 30 天。
"""
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from db.dao import Dao


@pytest.fixture(scope='module')
def dao():
    d = Dao()
    yield d
    d.close()


def test_snapshot_retention_is_short(dao):
    """cleanup_live 的快照保留期不能超过 7 天。

    30 天会让表涨到几十 G；3~7 天足够覆盖「判定延迟」的场景。
    """
    src = open(os.path.join(BASE, 'db', 'dao.py'), encoding='utf-8').read()
    idx = src.find('def cleanup_live')
    assert idx > 0
    body = src[idx:idx + 2500]
    assert 'INTERVAL 30 DAY' not in body, \
        'live_snapshot 仍保留 30 天，会再次把磁盘写满'


def test_snapshot_cleanup_protects_referenced(dao):
    """清理必须排除被 arbitrage_snapshot 引用的快照。

    那些是命中证据链的一环，删了回放和证据页都会失效。
    """
    src = open(os.path.join(BASE, 'db', 'dao.py'), encoding='utf-8').read()
    idx = src.find('def cleanup_live')
    body = src[idx:idx + 3000]
    assert ('kessbet_arbitrage_snapshot' in body
            or 'evidence_id' in body), \
        '清理 live_snapshot 时没有排除被引用的快照'


def test_dao_has_snapshot_cleanup_sql():
    """确认清理语句实际存在（不是只改注释）。"""
    src = open(os.path.join(BASE, 'db', 'dao.py'), encoding='utf-8').read()
    idx = src.find('def cleanup_live')
    body = src[idx:idx + 3000]
    assert 'DELETE FROM kessbet_live_snapshot' in body
