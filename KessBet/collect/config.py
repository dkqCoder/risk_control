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


# ---------------------------------------------------------------------------
# 滚球采集
# ---------------------------------------------------------------------------
# 总开关。关闭时完全不启动滚球采集线程。
LIVE_ENABLED = True

# 滚球**套利判定**的独立开关。
#
# 与 LIVE_ENABLED 分开是必要的：采集到的赔率有长期价值（复盘、重跑模型），
# 判定却可能因为模型有问题需要单独停掉。2026-11-13 就是这种情况 ——
# 结果盘判定产生 869 条假信号（成本低到 0.02，2 个选项的组合不可能
# 覆盖全部比赛结果），但采集数据本身没问题。
#
# 关掉后：滚球赔率/状态照常采集，只是不再算覆盖成本、不写 arbitrage_snapshot。
LIVE_ANALYZE_ENABLED = False

# 比赛开赛后继续采多久。足球 90 分钟 + 中场 15 分 + 补时 ≈ 110 分钟，
# 150 留了 40 分钟余量（延误、伤停、加时）。
# ⚠️ 这个值没有实测依据（实测样本里最晚一场 ms=7 是开赛后 70 分钟），
#    上线后应该用真实数据校准。
LIVE_WINDOW_MIN = 150

# 状态轮询间隔（比分/角球/红黄牌）。比分变化不频繁，10 秒够用。
LIVE_POLL_STATE_SEC = 10

# 赔率轮询间隔。
# ⚠️ 这个值是**临时的**：如果验证确认平台会通过 WS 推送滚球赔率，
#    就改成推送驱动，不轮询；否则用这个间隔轮询。
# 实测接口耗时 fetch_raw 平均 0.71s，30 秒间隔下 20 场并发约 0.67 QPS。
LIVE_POLL_ODDS_SEC = 30

# 滚球数据保留天数。比赛结束后基本没有分析价值，比赛前（7 天）短。
LIVE_RETAIN_DAYS = 3

# 滚球采集并发数。滚球比赛同时进行的场次有限，不需要像赛前那么大。
LIVE_CONCURRENCY = 8

# 滚球套利判定的轮询间隔。
#
# 和赛前一样是 2 秒：判定本身只查「赔率有没有变」（live_analyze_pending），
# 没变时一次 SQL 就返回，开销可以忽略；变了就要尽快算出来 ——
# 滚球套利的机会窗口只有几秒，慢半分钟就没了。
#
# ⚠️ 这个线程必须独立于赛前分析：赛前全量扫一次要 410 秒，
# 混在一个循环里会让滚球判定被堵住好几分钟（实测 2026-11-12）。
LIVE_ANALYZE_SEC = 2

# 滚球赔率新鲜度阈值（秒）。最后更新距今超过这个值，跳过判定。
#
# 实测反例（2026-11-12，Thailand 0:2）：
#   1x2 X @8.25 是 890 秒前采的
#   Double Chance 12 @1.14 是 1328 秒前的
#   平台早已停盘，分析拿了过期数据算出 Σ=0.9984 的假套利。
#
# 90 秒够覆盖「半场/暂停/门球」等正常间隙，但排掉「平台已停盘」。
LIVE_ODDS_MAX_AGE_SEC = 90

# 从滚球列表接口（00232）拉哪些运动。
#
# 为什么要单独一份：赛程接口(00252)只返回未开赛的比赛，滚球要靠
# 00232 才拉得到。实测 2026-11-12 足球 3 场、篮球 15 场。
# 这里只列**要补采**的运动 —— 拉多了只是多几个接口调用，
# 但会让 upsert 写入一堆我们暂时不分析的比赛。
LIVE_SPORTS = ('sr:sport:1', 'sr:sport:2')

# 滚球列表的刷新间隔（秒）。
#
# 比赛程的 1 小时密得多：滚球比赛随时可能开始/结束，
# 而且新开赛的比赛如果这次没补进来，就要等下一轮。
# 60 秒足够 —— 一场比赛踢 90 分钟，晚 1 分钟进库不影响分析。
LIVE_SCHEDULE_SEC = 60
