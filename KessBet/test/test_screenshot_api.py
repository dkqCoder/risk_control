"""截图必须能在页面上看到。

背景（2026-11-21）：截图功能跑通后文件只落在 data/screenshots/，
没有接口暴露、也没有页面引用 —— 只有 SSH 到服务器才能看。

契约：
  - /api/snapshots/{id}/screenshots 列出该命中的截图文件名
  - /screenshots/{filename} 能直接取到 PNG
  - 只允许取 data/screenshots 下的文件，不能路径穿越
"""
import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from fastapi.testclient import TestClient

from web.api import app


@pytest.fixture
def client():
    return TestClient(app)


def test_list_screenshots_for_snapshot(client):
    """列某次命中关联的截图。

    文件名格式 {时间戳}_{match_id}_{snapshot_id}_local.png，
    按 snapshot_id 后缀匹配。
    """
    r = client.get('/api/snapshots/1/screenshots')
    assert r.status_code == 200
    d = r.json()
    assert 'files' in d
    assert isinstance(d['files'], list)


def test_serve_screenshot_file(client):
    """能按文件名取 PNG。"""
    shot_dir = os.path.join(BASE, 'data', 'screenshots')
    os.makedirs(shot_dir, exist_ok=True)
    name = '__test_serve.png'
    path = os.path.join(shot_dir, name)
    # 1x1 PNG
    png = bytes.fromhex(
        '89504e470d0a1a0a0000000d4948445200000001000000010806000000'
        '1f15c4890000000d49444154789c6360000002000100ffff0300000600'
        '0557bfabd40000000049454e44ae426082')
    with open(path, 'wb') as f:
        f.write(png)
    try:
        r = client.get(f'/screenshots/{name}')
        assert r.status_code == 200
        assert r.headers['content-type'] in ('image/png', 'image/png; charset=utf-8')
        assert r.content == png
    finally:
        os.remove(path)


def test_reject_path_traversal(client):
    """不能透过 /screenshots/ 读别的目录。"""
    for bad in ('../deploy/.env', '..%2Fdeploy%2F.env', '/etc/passwd'):
        r = client.get(f'/screenshots/{bad}')
        assert r.status_code in (400, 403, 404), \
            f'{bad} 未被拒绝，返回 {r.status_code}'


def test_missing_screenshot_404(client):
    r = client.get('/screenshots/__no_such_file__.png')
    assert r.status_code == 404
