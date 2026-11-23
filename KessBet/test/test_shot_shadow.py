"""影子命中也要截图 + 截图 URL 指向正确端口。

背景（2026-11-21 验证）：
  1. `_shoot_async` 只在正式命中分支调用，影子分支（shadow=True）从不截图。
     但影子模式正是用来验证流程的 —— 不截图就验证不了截图功能。
  2. `LIVE_SHOT_BASE_URL` 写死 8080，实际 API 在 9001，
     本地证据页截图会拍到「连接失败」。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from collect import config


def test_shot_base_url_matches_actual_api_port():
    """截图用的本地 URL 必须指向 API 实际监听端口。"""
    url = config.LIVE_SHOT_BASE_URL
    assert ':8080' not in url, \
        f'LIVE_SHOT_BASE_URL={url} 端口错误，API 实际在 9001'


def test_shadow_branch_calls_shoot():
    """影子命中分支必须调用 _shoot_async。

    用源代码检查而不是 mock 整条链路 —— 判定流程依赖数据库和真实数据，
    但「影子分支里有没有这行调用」是稳定可查的。
    """
    src = open(os.path.join(BASE, 'analyze', 'live', 'run.py'),
               encoding='utf-8').read()

    idx = src.find('shadow=True)')
    assert idx > 0, '找不到影子命中落表调用'

    # 影子分支到 else 之间必须出现 _shoot_async。
    # 只截到 `else:` 为止 —— 否则会误把正式分支的调用算进来。
    tail = src[idx:idx + 800]
    else_pos = tail.find('\n        else:')
    assert else_pos > 0, '找不到影子分支的结束位置'
    shadow_segment = tail[:else_pos]
    assert '_shoot_async' in shadow_segment, \
        f'影子命中分支没有调用 _shoot_async；该分支内容:\n{shadow_segment}'


def test_shot_enabled_by_default():
    assert getattr(config, 'LIVE_SHOT_ENABLED', False) is True


def test_shot_dir_exists_or_creatable():
    d = config.LIVE_SHOT_DIR
    assert d, 'LIVE_SHOT_DIR 不能为空'
    assert os.path.isdir(os.path.dirname(d)), \
        f'{d} 的父目录不存在'
