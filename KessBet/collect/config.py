"""collect/config.py — 采集侧静态配置。"""
import os

PLATFORM = 'kessbet'

# Sportradar 运动编号 -> 平台 tabId
SPORTS = {
    'sr:sport:1':  1,    # 足球
    'sr:sport:2':  8,    # 篮球
    'sr:sport:3': 13,    # 棒球
    'sr:sport:6': 16,    # 手球
    'sr:sport:16': 44,   # 美式足球
    'sr:sport:29': 47,   # 班迪
    'sr:sport:10': 79,   # 格斗
}

# 反向映射，解析赛程时用
TAB_TO_SPORT = {v: k for k, v in SPORTS.items()}

# 首期只分析足球
ANALYZE_SPORTS = {'sr:sport:1'}

PROXY = os.environ.get('KESSBET_PROXY', 'http://127.0.0.1:7897')
HTTP_TIMEOUT = 30.0
HTTP_RETRIES = 2
HTTP_CONCURRENCY = 20

MAX_PAGES = 40            # 赛程游标翻页上限（实测 18 页）

REFRESH_CONCURRENCY = 20
REFRESH_DEBOUNCE_SEC = 5.0
REFRESH_MIN_RATIO = 0.3   # 接口返回数低于库内数 30% 视为残缺，不覆盖

WS_HEARTBEAT_INTERVAL = 20
WS_RECONNECT_BACKOFF_MAX = 60
WS_SUBSCRIBE_BATCH = 30
WS_URL = ('wss://socketio.kessbet.com/cpop/network/'
          '?brand=KYA&platform=pc&EIO=3&transport=websocket')

ANALYZE_INTERVAL = 60
ANALYZE_HORIZON_HOURS = 6
