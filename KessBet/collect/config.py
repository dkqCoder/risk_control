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

# ---- 分析进程调度 ----
# 事件驱动：每 ANALYZE_POLL_SEC 秒查一次「赔率比快照新」的比赛，只算这些。
# 赔率由采集侧写入 odds_prematch，updated_time 一变就会被这里捕获。
ANALYZE_POLL_SEC = 2

# 兜底：每 N 秒忽略「没变」判断，全量算一遍。
# 防快照表被误删、时钟漂移、跳过条件写错导致某些场次永远不更新。
ANALYZE_FULL_SWEEP_SEC = 300

# 并发线程数。ILP 是 CPU 密集的，设成 CPU 核数的一半左右比较稳，
# 太高会和采集进程抢 CPU。
ANALYZE_CONCURRENCY = 4

# 采集与分析的时间窗口（小时）。三处必须一致：
#   - 采集 bootstrap：拉这些比赛的赔率
#   - WS 订阅：订阅这些比赛接收变动推送
#   - 分析：只算这些比赛的覆盖成本
# 窗口外的比赛只有赛程、没有赔率，列表页会显示「无赔率」。
HORIZON_HOURS = 72

ANALYZE_HORIZON_HOURS = HORIZON_HOURS

# 兼容旧配置名（run_loop 的默认 interval 走 ANALYZE_POLL_SEC）
ANALYZE_INTERVAL = ANALYZE_POLL_SEC
