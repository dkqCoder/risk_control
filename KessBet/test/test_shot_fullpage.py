"""平台截图必须覆盖全部盘口。

背景（2026-11-21）：点 All tab 后截图只截到视口高度，页面下方大量盘口
（Total 的深盘线、Correct Score、Half Time 相关）被截断。
但不能用 captureBeyondViewport —— 实测那样截出来中间是大片空白。

正确做法：先量 document 实际高度，再用这个高度重开浏览器 / 设视口，
最后截一次。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)


def test_capture_measures_page_height():
    """capture_match_meta 必须读取 document 高度。"""
    src = open(os.path.join(BASE, 'common', 'platform_shot.py'),
               encoding='utf-8').read()
    assert 'scrollHeight' in src or 'offsetHeight' in src, \
        '没有读取 document 高度，无法保证拍全全部盘口'


def test_capture_resizes_to_full_height():
    """必须根据量到的高度调整截图高度。"""
    src = open(os.path.join(BASE, 'common', 'platform_shot.py'),
               encoding='utf-8').read()
    # 应出现设置视口/窗口尺寸的调用
    assert ('Emulation.setDeviceMetricsOverride' in src
            or 'window.innerHeight' in src
            or 'set_device_metrics' in src), \
        '没有按页面高度调整视口，截图会被截断'


def test_no_capture_beyond_viewport():
    """仍不能使用 captureBeyondViewport（会出空白）。

    只检查实际调用，不看文档字符串 —— 注释里提到这个参数是说明为什么不用。
    """
    src = open(os.path.join(BASE, 'common', 'platform_shot.py'),
               encoding='utf-8').read()
    # 找 Page.captureScreenshot 的实际调用块
    for i, line in enumerate(src.splitlines(), 1):
        if 'captureBeyondViewport' not in line:
            continue
        stripped = line.strip()
        # 注释/文档里提到不算
        if stripped.startswith('#') or '不能用' in line or '⚠️' in line:
            continue
        raise AssertionError(
            f'第 {i} 行在调用里使用了 captureBeyondViewport：{stripped}')
