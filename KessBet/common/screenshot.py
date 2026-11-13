"""common/screenshot.py — 用系统 Chrome 无头模式截网页。

## 为什么用命令行而不是 playwright

Python 版的 playwright 在本机启动时 `greenlet` 的 DLL 加载失败
（`ImportError: DLL load failed ... 拒绝访问`），换了几次环境都不行。
系统自带 Chrome 的 `--headless --screenshot` 更简单，也没有额外依赖。

## `--virtual-time-budget` 的作用

我们的页面是「加载后 fetch 数据再渲染」，单纯截图会拍到空白表格。
`--virtual-time-budget=6000` 让浏览器把 JS 和网络请求都推进 6 秒
虚拟时间后再截，足够 fetch 完成。

## 失败不抛异常

截图是辅助证据，不是主流程。它挂了不能影响套利判定本身。
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile

log = logging.getLogger(__name__)

# 常见的 Chrome/Edge 安装位置。用第一个存在的。
_CANDIDATES = [
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
]

_browser_cache: dict[str, str | None] = {}


def find_browser() -> str | None:
    if 'path' in _browser_cache:
        return _browser_cache['path']
    found = None
    for p in _CANDIDATES:
        if os.path.isfile(p):
            found = p
            break
    _browser_cache['path'] = found
    if not found:
        log.warning('未找到 Chrome/Edge，网页截图功能不可用')
    return found


def capture(url: str, out_path: str, width: int = 1500,
            height: int = 1600, wait_ms: int = 6000,
            timeout: int = 45) -> tuple[bool, str]:
    """截一张图。返回 (成功?, 说明)。

    在 Windows 上 subprocess 要隐藏控制台窗口，否则每截一次图
    都会闪一个黑框。
    """
    browser = find_browser()
    if not browser:
        return False, 'no_browser'

    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)

    creationflags = 0
    if os.name == 'nt':
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

    cmd = [
        browser,
        '--headless=new',
        '--disable-gpu',
        '--no-sandbox',
        '--hide-scrollbars',
        '--force-device-scale-factor=1',
        f'--window-size={width},{height}',
        f'--virtual-time-budget={wait_ms}',
        f'--screenshot={out_path}',
        url,
    ]

    # Chrome 有时会把截图写到奇怪的临时目录，先删掉旧文件
    try:
        if os.path.exists(out_path):
            os.remove(out_path)
    except OSError:
        pass

    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=creationflags)
    except subprocess.TimeoutExpired:
        return False, 'timeout'
    except Exception as e:
        return False, str(e)

    if os.path.isfile(out_path) and os.path.getsize(out_path) > 1024:
        return True, out_path

    err = (r.stderr or b'').decode('utf-8', 'ignore')[-300:]
    log.warning('截图失败 %s: %s', out_path, err)
    return False, err or 'no_file'


def capture_to_temp(url: str, name: str, **kw) -> tuple[bool, str]:
    """写到系统临时目录。开发/调试用（项目目录可能被沙箱拦）。"""
    path = os.path.join(tempfile.gettempdir(), name)
    return capture(url, path, **kw)
