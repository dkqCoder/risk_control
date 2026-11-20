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
#
# 2026-11-15 重新打开：假信号的根因（缺腿盘口、赔率滞后于比分、混合新旧行）
# 已由 LIVE_DROP_INCOMPLETE_MARKETS / score_newer_than_odds / LIVE_ROW_MAX_LAG_SEC
# 三道防线覆盖。观察期若再出现成本 < 0.9 的命中，先查原始数据再决定是否再关。
# 2026-11-17 影子验收：开启分析，但 LIVE_SHADOW_MODE=True 只写影子状态/
# 证据包，不写正式命中、不发正式机会告警。正式发布保持关闭，直到官网
# 取证和真实样本验收完成。
LIVE_ANALYZE_ENABLED = True

# 影子模式：独立于“是否运行分析”。开启时照常计算、写影子状态和证据包，
# 但不写正式套利命中、不发正式机会告警。正式发布需同时设为 False。
LIVE_SHADOW_MODE = True

# 是否允许 Correct Score 的 `other`（其他比分）参与认证。
#
# 默认 False，且**不要轻易打开**。
#
# `other` 的覆盖范围是“具名比分之外的补集”，但平台在临近结束时会锁定
# 或撤下当前比分（例如 3:0）。我们的解析器只保留可售选项，锁定项被丢弃，
# 补集就会把 3:0 算进去。实测（2026-11-17 Switzerland vs Scotland）：
#   3:0 在 05:24:58 还有 1.50，05:26:23 被锁/撤；
#   other 当时是 24/40/100 —— 如果它包含 3:0（1.50 ≈ 67%），赔率不可能这么高。
#   程序把 3:0 补回 other，和 Total 3.5 over 拼出 Σ=0.33 的假套利。
#
# 只有拿到平台明确的结算规则（“其他”到底覆盖哪些比分）后才允许恢复。
LIVE_CERTIFY_CORRECT_SCORE_OTHER = False

# 输入快照的解析/规则版本。影响输入指纹和证据回放；改动解析语义时要升。
LIVE_PARSER_VERSION = 'kessbet-parse-v1'
LIVE_RULES_VERSION = 'live-rules-v10'

# 认证配资：预算必须能被最小下注单位整除；金额按单位取整后复算最坏返还。
LIVE_CERT_TOTAL_STAKE = 10000
LIVE_CERT_STAKE_UNIT = 1
LIVE_CERT_MIN_PROFIT = 0.0      # 最低净收益率门槛；即使 0 也要求严格正收益

# 滚球「算作套利」的成本阈值。
#
# 1.0 —— 只有 Σ(1/赔率) < 1 才是数学意义上的无损套利。
#
# 2026-11-20 曾临时设为 1.05 验证下游流程（命中落表、证据包、截图、
# 复核报表）。验证完成后已改回 1.0；相关影子脏数据同时清理。
# 以后再需要验证时，记得连带清掉放宽阈值期间产生的记录。
#
# 注意：调高**不会绕过无界验证**。cost < 1.0 仍然要求 verify 在无界
# 比分域上找不到亏损终局；能找到反例的照样不算命中。
LIVE_ARB_COST_THRESHOLD = 1.0

# 比赛开赛后继续采多久。足球 90 分钟 + 中场 15 分 + 补时 ≈ 110 分钟，
# 150 留了 40 分钟余量（延误、伤停、加时）。
# ⚠️ 这个值没有实测依据（实测样本里最晚一场 ms=7 是开赛后 70 分钟），
#    上线后应该用真实数据校准。
LIVE_WINDOW_MIN = 150

# 状态轮询间隔（比分/角球/红黄牌）。比分变化不频繁，10 秒够用。
LIVE_POLL_STATE_SEC = 5

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

# 是否启用「盘口内部和异常就丢弃」的防线。
#
# 默认关闭（2026-11-13）：还没确定那些异常报价到底是平台真的放出来的、
# 还是我们解析错的。在搞清之前先不拦，让数据原样落库，
# 用 tools/probe_live_raw_odds.py 对比原始串。
#
# 根因确认后再决定是否打开。
LIVE_DROP_BROKEN_MARKETS = False

# 剔除「缺腿」的盘口（默认开启）。
#
# 平台经常锁掉一条腿（原始串标记 `,0,`），只留另一条。剩下的那条价格
# 是冻结的 —— 锁腿通常意味着这个盘口马上要结算。拿冻结价格去拼覆盖，
# 得到的是下不了注的「套利」。
#
# 实测（2026-11-13，231 轮滚球采样）：127 个内部和异常的盘口
# **全部**是这种形态，没有一个是解析错误。
#
# 和 LIVE_DROP_BROKEN_MARKETS 的区别：
#   那个按「内部和 < 阈值」判断（统计口径，可能误伤）
#   这个按「标签集合是否齐全」判断（结构性，判据确定）
LIVE_DROP_INCOMPLETE_MARKETS = True

# 单场内部的数据一致性阈值（秒）。
#
# odds_updated 取的是 MAX(updated_time) —— 只要有一个盘口在推就算新鲜。
# 但同一场比赛里可能有些盘口早就停推了，它们的旧行一直留在表里：
#   实测（2026-11-13）同场新旧赔率行相差 8354 秒（2.3 小时）
#
# 拿「1 秒前的 1x2」配「50 分钟前的 Handicap」算覆盖，会得到 Σ=0.05
# 这种荒谬套利 —— 那两个价格从来没有同时在平台上存在过。
#
# 120 秒：一次正常的推送周期内，接口返回的所有选项都会一起刷新。
# 超过 2 分钟没刷新的，说明最近那次响应里根本没有它。
LIVE_ROW_MAX_LAG_SEC = 120

# 命中套利时自动截图留证。
#
# 滚球赔率几秒就变，命中那一刻的页面状态事后无法复现 ——
# 截一张图，方便人工复核「当时到底是什么情况」。
#
# 截图内容是本地详情页 /live/<match_id>，包含计分板、判定结果、
# 下注组合和当时的全部盘口。
LIVE_SHOT_ENABLED = True
LIVE_SHOT_BASE_URL = 'http://127.0.0.1:8080'
# 存在工作区的 data/screenshots/ 下（data/ 已在 .gitignore 里，
# 截图是二进制证据，不该进版本库）
LIVE_SHOT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'screenshots')

# E1 证据包目录（不可变，按 evidence_id 一包一目录；data/ 已在 .gitignore）。
LIVE_EVIDENCE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'evidence')

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
