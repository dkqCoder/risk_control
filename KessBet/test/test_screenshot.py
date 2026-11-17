"""套利截图留证的测试。

不真的启动浏览器（慢、依赖环境），只验证：
  - 开关关闭时不做任何事
  - 路径拼接正确
  - 找不到浏览器时优雅失败
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import pytest

from common import screenshot


def test_find_browser_returns_path_or_none():
    """找到浏览器返回绝对路径，找不到返回 None（不能抛异常）。"""
    p = screenshot.find_browser()
    assert p is None or os.path.isabs(p)


def test_capture_fails_gracefully_without_browser(monkeypatch):
    """没有浏览器时返回 (False, ...)，不能抛异常。"""
    monkeypatch.setattr(screenshot, 'find_browser', lambda: None)
    ok, msg = screenshot.capture('http://127.0.0.1:1/', '/tmp/x.png')
    assert ok is False
    assert msg == 'no_browser'


def test_shoot_async_disabled_does_nothing(monkeypatch):
    """开关关闭时不启动线程、不产生文件。"""
    from collect import config
    from analyze.live import run as L

    monkeypatch.setattr(config, 'LIVE_SHOT_ENABLED', False)
    called = []
    monkeypatch.setattr(screenshot, 'capture',
                        lambda *a, **k: called.append(a) or (True, 'x'))

    L._shoot_async({'match_id': 'm1'}, {'cost': 0.9}, 1)
    assert called == [], '开关关闭时不该截图'


def test_shoot_async_captures_platform_and_local(monkeypatch, tmp_path):
    """命中时截两张：平台官网 + 本地详情页。

    平台图是判断「套利真不真」的第一手证据 —— 库里的赔率是采来的，
    平台页面才是最终事实。本地图展示我们算出的下注组合。
    """
    import time
    from collect import config
    from analyze.live import run as L
    from common import platform_shot

    monkeypatch.setattr(config, 'LIVE_SHOT_ENABLED', True)
    monkeypatch.setattr(config, 'LIVE_SHOT_DIR', str(tmp_path))
    monkeypatch.setattr(config, 'LIVE_SHOT_BASE_URL', 'http://x')

    got = {}
    monkeypatch.setattr(
        platform_shot, 'capture_match_meta',
        lambda mid, path, **k: got.update(platform=(mid, path)) or {
            'ok': True, 'path': path, 'tab': 'all', 'coverage': 'all',
            'reason': None})
    monkeypatch.setattr(
        screenshot, 'capture',
        lambda url, path, **k: got.update(local=(url, path)) or (True, path))

    L._shoot_async({'match_id': '245123456789'}, {'cost': 0.9}, 486)
    for _ in range(100):
        if len(got) == 2:
            break
        time.sleep(0.02)

    assert 'platform' in got, '没有截平台官网'
    assert got['platform'][0] == '245123456789'
    assert os.path.basename(got['platform'][1]).endswith(
        '_245123456789_486_kessbet.png')

    assert 'local' in got, '没有截本地证据页'
    assert got['local'][0] == 'http://x/evidence/486'
    assert os.path.basename(got['local'][1]).endswith(
        '_245123456789_486_local.png')

    name = os.path.basename(got['platform'][1])
    assert name[:8].isdigit() and name[8] == '-', '时间戳格式应为 YYYYMMDD-HHMMSS'
