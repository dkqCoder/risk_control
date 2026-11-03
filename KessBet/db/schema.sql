-- =============================================================================
-- KessBet 赔率监控 · 数据库 DDL
-- =============================================================================
-- 目标库：sssj @ 10.180.8.110（MySQL 5.7.32）
--
-- 执行前确认：
--   SELECT VERSION();                 -- 5.7.32
--   SHOW VARIABLES LIKE 'time_zone';  -- +08:00
--
-- 字符集：utf8mb4（服务器默认 utf8 只支持 3 字节，队名可能有 emoji/特殊字符）
--
-- ⚠️ 所有 DATETIME 字段存的是**北京时间**（UTC+8），不带时区标记。
--    写入统一走 common/timeutil.py 的 to_db()，不要直接塞 UTC。
--    SQL 里比较时间用 NOW()（服务器时区 +08:00），不要用 UTC_TIMESTAMP()。
--
-- 五张表的分工：
--   kessbet_match          赛程维表，一场一行
--   kessbet_odds_prematch  当前赔率状态，分析侧唯一读取源，一个选项一行
--   kessbet_odds_history   赔率变动流水，只增不改，保留 7 天
--   kessbet_odds_hit       套利命中记录（cost < 1 时写）
--   kessbet_collect_log    采集/分析运行日志
-- =============================================================================

-- =============================================================================
-- sport 字段取值（Sportradar 运动编号）
-- -----------------------------------------------------------------------------
--   sr:sport:1   足球        Soccer          tabId=1    盘口 71~95 个
--   sr:sport:2   篮球        Basketball      tabId=8    盘口 11 个
--   sr:sport:3   棒球        Baseball        tabId=13   盘口 3 个
--   sr:sport:6   手球        Handball        tabId=16   盘口 1 个
--   sr:sport:10  格斗        Combat Sports   tabId=79   盘口 6 个
--   sr:sport:16  美式足球    American Football tabId=44 盘口 25 个
--   sr:sport:29  班迪        Bandy           tabId=47   盘口 2 个
--
-- 为什么用 Sportradar 编号而不是 tabId：
--   tabId 是平台内部编号，不连续、可能变；sport 编号是行业标准，
--   将来接第二个平台时，两边的 sport 可以直接对齐。
--
-- 定义见 collect/config.py 的 SPORTS 字典。
-- 首期只分析足球（sr:sport:1），其余运动只采集不判定。
-- =============================================================================

SET NAMES utf8mb4;


-- =============================================================================
-- ① kessbet_match — 赛程维表
-- -----------------------------------------------------------------------------
-- 一场比赛一行。每小时从平台列表接口刷新一次。
-- 不删数据，靠 cleanup() 按开赛时间 + 7 天清理。
-- =============================================================================
CREATE TABLE IF NOT EXISTS kessbet_match (
    platform        VARCHAR(16)  NOT NULL
        COMMENT '平台标识，当前固定 kessbet',

    match_id        VARCHAR(32)  NOT NULL
        COMMENT '平台内部比赛 ID（19 位数字串）。拉盘口接口用这个',

    sport           VARCHAR(16)  NOT NULL
        COMMENT 'Sportradar 运动编号，取值 sr:sport:N，对照表见文件头',

    canonical_id    VARCHAR(64)  NOT NULL DEFAULT ''
        COMMENT '跨平台统一比赛 ID（预留，当前恒为空）。将来多平台对齐时填',

    sportradar_id   VARCHAR(64)  NOT NULL DEFAULT ''
        COMMENT 'Sportradar 比赛 ID，格式 sr:match:NNNNN。实测 100% 覆盖，多平台对齐的关键',

    league          VARCHAR(128) NOT NULL DEFAULT ''
        COMMENT '联赛名，平台原文（英文）。如 Premier League / NBA / MLB',

    country         VARCHAR(64)  NOT NULL DEFAULT ''
        COMMENT '国家/地区，平台原文。如 England / International Clubs',

    home_team       VARCHAR(128) NOT NULL DEFAULT ''
        COMMENT '主队名，平台原文。如 Arsenal / Charlotte Hornets',

    away_team       VARCHAR(128) NOT NULL DEFAULT ''
        COMMENT '客队名，平台原文。如 Everton / Washington Wizards',

    kickoff_time    DATETIME     NOT NULL
        COMMENT '开赛时间（北京时间）。由接口 mbtp 毫秒时间戳换算，最可靠的时间字段',

    first_seen_time DATETIME     NOT NULL
        COMMENT '首次发现该比赛的时间（北京时间）。刷新不覆盖，用于统计比赛提前多久被开出',

    updated_time    DATETIME     NOT NULL
        COMMENT '最后一次刷新赛程的时间（北京时间）。每小时更新一次',

    PRIMARY KEY (platform, match_id),
    KEY idx_kickoff (kickoff_time)
        COMMENT '按开赛时间筛选（分析侧取未来 N 小时）',
    KEY idx_sport_kickoff (sport, kickoff_time)
        COMMENT '按运动+时间筛选（bootstrap 按运动拉赔率）',
    KEY idx_sportradar (sportradar_id)
        COMMENT '跨平台对齐时按 Sportradar ID 查找'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='赛程维表：一场比赛一行，每小时刷新';


-- =============================================================================
-- ② kessbet_match_canonical — 跨平台比赛映射
-- -----------------------------------------------------------------------------
-- 一场真实世界的比赛在多个平台各有自己的 match_id。这张表把它们对齐到
-- 一个统一键，供跨平台比价和页面聚合使用。
--
-- 对齐依据：sportradar_id（sr:match:NNNNN）。
--   实测 kessbet 的 4440 场 100% 带该字段，且无重复 —— 可以直接做关联键。
--   某平台若没有 sportradar_id，退回 (开赛时间 ± 容差, 主队名归一化, 客队名归一化)
--   做模糊匹配，匹配结果写入 match_key 但不写主键约束。
--
-- ⚠️ 这张表**不参与**单平台套利判定。单平台分析仍按 (platform, match_id) 走。
--    只有跨平台比价和前端聚合才需要它。
-- =============================================================================
CREATE TABLE IF NOT EXISTS kessbet_match_canonical (
    canonical_id   VARCHAR(64)  NOT NULL
        COMMENT '统一比赛键。优先用 sportradar_id；没有则用 fallback:<hash>',

    platform       VARCHAR(16)  NOT NULL
        COMMENT '平台标识，如 kessbet',

    match_id       VARCHAR(32)  NOT NULL
        COMMENT '该平台内部的比赛 ID',

    sport          VARCHAR(16)  NOT NULL
        COMMENT 'Sportradar 运动编号，取值 sr:sport:N',

    match_source   VARCHAR(16)  NOT NULL DEFAULT 'sportradar'
        COMMENT 'canonical_id 的来源：sportradar=按 ID 对齐 / fallback=按队名+时间模糊匹配',

    home_team      VARCHAR(128) NOT NULL DEFAULT ''
        COMMENT '主队名，平台原文。多平台写法可能不同，聚合时取第一个平台的作为展示名',

    away_team      VARCHAR(128) NOT NULL DEFAULT ''
        COMMENT '客队名，平台原文',

    kickoff_time   DATETIME     NOT NULL
        COMMENT '开赛时间（北京时间）。多平台可能有分钟级差异',

    updated_time   DATETIME     NOT NULL
        COMMENT '最后一次写入时间（北京时间）',

    PRIMARY KEY (platform, match_id),
    KEY idx_canonical (canonical_id)
        COMMENT '按统一键聚合该场比赛的所有平台报价（页面主查询）',
    KEY idx_kickoff (kickoff_time)
        COMMENT '按开赛时间清理'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='跨平台比赛映射：一个真实比赛多行，每平台一行';


-- =============================================================================
-- ③ kessbet_odds_prematch — 赛前赔率当前状态
-- -----------------------------------------------------------------------------
-- 分析侧唯一的读取源。一个选项一行，同一选项永远只有最新值。
--
-- 写入方式：整场替换
--   1. upsert 本次接口返回的全部选项
--   2. 删除库里有、接口没返回的行（说明平台撤盘了）
--
-- 为什么不用 history 表直接查当前值：
--   单场历史约 12000 行（7 天累积），而当前状态只有 240 行。
--   MySQL 5.7 没有窗口函数，查"每个选项的最新值"要自连接聚合，慢 50 倍。
-- =============================================================================
CREATE TABLE IF NOT EXISTS kessbet_odds_prematch (
    platform     VARCHAR(16)  NOT NULL
        COMMENT '平台标识，当前固定 kessbet',

    match_id     VARCHAR(32)  NOT NULL
        COMMENT '比赛 ID，关联 kessbet_match.match_id',

    sport        VARCHAR(16)  NOT NULL
        COMMENT 'Sportradar 运动编号，取值 sr:sport:N，对照表见文件头',

    market       VARCHAR(64)  NOT NULL
        COMMENT '归一化盘口名（已剥离队名前缀和半场前缀之后的部分）。
                 示例：1x2 / Total / Handicap / Correct Score / 1st Half - Total
                 注意：队名盘口的队名被拆到 side 字段，不在此处',
                 -- 超长会截断，实测最长 65 字符（长队名 + 长盘口名）

    side         CHAR(1)      NOT NULL DEFAULT ''
        COMMENT '盘口归属：H=主队专属 / A=客队专属 / 空=全场或通用。
                 来源：盘口名带主队前缀（如 Arsenal Total）→ H
                 例：market=Total, side=H 表示"阿森纳自己的进球数"',

    line         VARCHAR(16)  NOT NULL DEFAULT ''
        COMMENT '盘线，三种格式：
                 数值型 2.5 / 3.5（Total 大小球）
                 主客型 0:1 / 1:0（Handicap 让球，主:客）
                 特殊 2up / 3up（1x2(xup) 领先球数）
                 比分盘 / 2up 之外的普通盘口为空',

    selection    VARCHAR(32)  NOT NULL
        COMMENT '选项标签，原样保留：
                 三项盘 1 / X / 2
                 大小球 over / under
                 是否类 yes / no
                 比分 0:0 / 2:1
                 组合 "X & yes" / "1/X & over"
                 半球单双 odd / even',

    odds         DECIMAL(8,3) NOT NULL
        COMMENT '当前十进制赔率。实测范围 1.00 ~ 101.00，最多 2 位小数，
                 存 3 位小数留余量。1.00 表示停售（标记位为 0）',

    prev_odds    DECIMAL(8,3) DEFAULT NULL
        COMMENT '上一次的赔率。upsert 时自动拿旧值填入（MySQL 的
                 ON DUPLICATE KEY UPDATE 从左到右求值）。
                 NULL 表示本次是首次出现。仅用于快速看价差，
                 完整变动历史在 kessbet_odds_history',

    kickoff_time DATETIME     NOT NULL
        COMMENT '开赛时间（北京时间）。冗余存储，避免分析时 join match 表',

    updated_time DATETIME     NOT NULL
        COMMENT '最后一次写入该选项的时间（北京时间）。可用于判断数据新鲜度',

    PRIMARY KEY (platform, match_id, market, side, line, selection),
    KEY idx_match (platform, match_id)
        COMMENT '按比赛取全部盘口（分析主查询）',
    KEY idx_kickoff (kickoff_time)
        COMMENT '按开赛时间清理过期数据',
    KEY idx_updated (updated_time)
        COMMENT '排查"哪些比赛很久没刷新"'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='赛前赔率当前状态：一个选项一行，整场替换写入';


-- =============================================================================
-- ④ kessbet_odds_history — 赔率变动历史
-- -----------------------------------------------------------------------------
-- 只增不改。每次刷新时，和当前状态表比对，只记录真正变化的选项。
-- 保留 7 天，按 kickoff_time 清理。
--
-- 实测数据量：约 3 万条变化/小时（400 场比赛在盯），
--             一天 70 万条，7 天约 500 万条 / 500 MB。
-- =============================================================================
CREATE TABLE IF NOT EXISTS kessbet_odds_history (
    id           BIGINT       NOT NULL AUTO_INCREMENT
        COMMENT '自增主键。不能用选项坐标做主键——同一选项会有多条记录',

    platform     VARCHAR(16)  NOT NULL
        COMMENT '平台标识，当前固定 kessbet',

    match_id     VARCHAR(32)  NOT NULL
        COMMENT '比赛 ID，关联 kessbet_match.match_id',

    sport        VARCHAR(16)  NOT NULL
        COMMENT 'Sportradar 运动编号，取值 sr:sport:N，对照表见文件头',

    market       VARCHAR(64)  NOT NULL
        COMMENT '归一化盘口名，含义同 kessbet_odds_prematch.market',

    side         CHAR(1)      NOT NULL DEFAULT ''
        COMMENT '盘口归属 H/A/空，含义同 kessbet_odds_prematch.side',

    line         VARCHAR(16)  NOT NULL DEFAULT ''
        COMMENT '盘线，含义同 kessbet_odds_prematch.line',

    selection    VARCHAR(32)  NOT NULL
        COMMENT '选项标签，含义同 kessbet_odds_prematch.selection',

    odds         DECIMAL(8,3) NOT NULL
        COMMENT '变化后的赔率',

    prev_odds    DECIMAL(8,3) DEFAULT NULL
        COMMENT '变化前的赔率。NULL 表示该选项首次出现（初始化时批量写入）。
                 有了这个字段，单条记录就自解释"从多少变到多少"，
                 不用去翻上一条记录',

    kickoff_time DATETIME     NOT NULL
        COMMENT '开赛时间（北京时间）。冗余存储，按此字段做 7 天清理',

    changed_time DATETIME     NOT NULL
        COMMENT '该次变化发生的时刻（北京时间），即采集时观察到变化的时刻。
                 不是平台改价的时刻（平台不提供该信息）',

    PRIMARY KEY (id),
    KEY idx_match (platform, match_id, changed_time)
        COMMENT '按比赛查价格曲线（时间有序）',
    KEY idx_changed (changed_time)
        COMMENT '按时间范围统计变化频率',
    KEY idx_kickoff (kickoff_time)
        COMMENT '按开赛时间清理过期数据'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='赔率变动历史：只增不改，保留 7 天；与 prematch 表互为快照/流水';


-- =============================================================================
-- ⑤ kessbet_odds_hit — 套利命中记录
-- -----------------------------------------------------------------------------
-- 只在 cost < 1.0 时写。正常情况下这张表**应该是空的**——
-- 单家博彩公司内部出现无损套利只可能是调价错误。
-- =============================================================================
CREATE TABLE IF NOT EXISTS kessbet_odds_hit (
    id           BIGINT       NOT NULL AUTO_INCREMENT
        COMMENT '自增主键',

    alert_time   DATETIME     NOT NULL
        COMMENT '检测到套利的时刻（北京时间）',

    platform     VARCHAR(16)  NOT NULL
        COMMENT '平台标识，当前固定 kessbet',

    match_id     VARCHAR(32)  NOT NULL
        COMMENT '比赛 ID，关联 kessbet_match.match_id',

    sport        VARCHAR(16)  NOT NULL
        COMMENT 'Sportradar 运动编号，取值 sr:sport:N，对照表见文件头',

    cost         DOUBLE       NOT NULL
        COMMENT '最小覆盖成本 Σ(1/赔率)。必须 < 1.0 才写。
                 用 DOUBLE 不用 DECIMAL —— DECIMAL(8,4) 会把 0.99996
                 四舍五入成 1.0000，与"严格 <1"的告警语义冲突',

    markets      TEXT         NOT NULL
        COMMENT '命中的最优组合，JSON 数组，每项形如 {"desc": "盘口|归属|盘线|选项"}' ,

    kickoff_time DATETIME     NOT NULL
        COMMENT '开赛时间（北京时间）',

    PRIMARY KEY (id),
    KEY idx_alert (alert_time)
        COMMENT '按告警时间查询',
    KEY idx_match (platform, match_id)
        COMMENT '按比赛查询'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='套利命中记录：只在 cost < 1 时写，正常应为空表';


-- =============================================================================
-- ⑥ kessbet_match_cost — 分析结果快照（供页面只读）
-- -----------------------------------------------------------------------------
-- analyze 每算完一场就 upsert 一行。页面读这张表，**不在请求里跑 ILP**。
--
-- 为什么必须有这张表：
--   单场 ILP 约 1.2 秒。列表页一屏 50 场就是 60 秒，用户不可能等。
--   分析进程本来就每 60 秒跑一轮，顺手把结果落库，页面读一次只要几毫秒。
--
-- 为什么按 match_id 而不是 canonical_id 存：
--   成本是**单平台内部**的概念（单家抽水）。跨平台成本要另外算，
--   将来会加 cost_cross 列并用 canonical_id 聚合。
-- =============================================================================
CREATE TABLE IF NOT EXISTS kessbet_match_cost (
    platform        VARCHAR(16)  NOT NULL
        COMMENT '平台标识',

    match_id        VARCHAR(32)  NOT NULL
        COMMENT '平台内比赛 ID',

    sport           VARCHAR(16)  NOT NULL
        COMMENT 'Sportradar 运动编号',

    kickoff_time    DATETIME     NOT NULL
        COMMENT '开赛时间（北京时间），冗余存储便于按时间筛选和清理',

    cost            DOUBLE       DEFAULT NULL
        COMMENT '最小覆盖成本 Σ(1/赔率)。NULL 表示没有可用候选或无法覆盖',

    cost_scope      VARCHAR(8)   NOT NULL DEFAULT 'single'
        COMMENT 'single=单平台内部最优。将来跨平台会写 cross',

    n_markets       INT          NOT NULL DEFAULT 0
        COMMENT '盘口数',

    n_selections    INT          NOT NULL DEFAULT 0
        COMMENT '选项数',

    n_candidates    INT          NOT NULL DEFAULT 0
        COMMENT '可建模的候选数（能映射到状态空间的）',

    ilp_chosen      TEXT
        COMMENT 'ILP 选中的选项描述，JSON 数组',

    lp_worst_profit DOUBLE       DEFAULT NULL
        COMMENT 'LP 最差状态净收益。t = 1/cost - 1，用于和 ILP 交叉校验',

    lp_chosen       TEXT
        COMMENT 'LP 建议的资金配置，JSON 数组 [{desc, weight}]',

    cross_gap       DOUBLE       DEFAULT NULL
        COMMENT 'ILP 与 LP 的偏差，正常应 < 1e-4',

    error           VARCHAR(32)  DEFAULT NULL
        COMMENT 'no_data / no_candidates / no_cover / ilp_error / lp_error；NULL 表示正常',

    odds_updated    DATETIME     DEFAULT NULL
        COMMENT '该场赔率最后一次变更时间。赔率没变就不必重算，analyze 用它跳过',

    computed_time   DATETIME     NOT NULL
        COMMENT '本次计算的时刻（北京时间）',

    PRIMARY KEY (platform, match_id),
    KEY idx_kickoff (kickoff_time)
        COMMENT '页面按开赛时间筛选',
    KEY idx_cost (cost)
        COMMENT '页面按成本排序/筛阈值',
    KEY idx_computed (computed_time)
        COMMENT '排查哪些比赛很久没算'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='分析结果快照：analyze 写入，页面只读，避免请求时跑 ILP';


-- =============================================================================
-- ⑦ kessbet_collect_log — 采集与运行日志
-- -----------------------------------------------------------------------------
-- 排查用。记录每次赛程刷新、清理、WS 连接等操作的结果。
-- 保留 30 天。
-- =============================================================================
CREATE TABLE IF NOT EXISTS kessbet_collect_log (
    id            BIGINT       NOT NULL AUTO_INCREMENT
        COMMENT '自增主键',

    platform      VARCHAR(16)  NOT NULL
        COMMENT '平台标识，当前固定 kessbet',

    event         VARCHAR(24)  NOT NULL
        COMMENT '事件类型。已使用：schedule（赛程刷新）/ refresh（赔率刷新）
                 / ws_connect / ws_disconnect / cleanup / analyze',

    started_time  DATETIME     NOT NULL
        COMMENT '事件开始时刻（北京时间）',

    finished_time DATETIME     DEFAULT NULL
        COMMENT '事件结束时刻（北京时间）。NULL 表示未完成（异常中断）',

    matches_count INT          NOT NULL DEFAULT 0
        COMMENT '该事件涉及的比赛数。schedule=刷新到的场次总数',

    rows_written  INT          NOT NULL DEFAULT 0
        COMMENT '该事件写入的行数',

    error         TEXT
        COMMENT '错误信息。NULL 表示成功',

    PRIMARY KEY (id),
    KEY idx_started (started_time)
        COMMENT '按时间范围查询日志',
    KEY idx_event (platform, event, started_time)
        COMMENT '按事件类型 + 时间查询（如"最近的 schedule 记录"）'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='采集与分析运行日志，保留 30 天';


-- =============================================================================
-- 验证与维护
-- =============================================================================
-- SHOW TABLES LIKE 'kessbet%';
-- SHOW CREATE TABLE kessbet_odds_prematch\G
--
-- 表规模查看：
--   SELECT table_name, table_rows,
--          ROUND(data_length/1024/1024, 1) AS data_mb,
--          ROUND(index_length/1024/1024, 1) AS index_mb
--   FROM information_schema.tables
--   WHERE table_schema='sssj' AND table_name LIKE 'kessbet%';
--
-- ⚠️ 改 COMMENT 必须 ALTER TABLE，MySQL 5.7 会重建整表（10 万行要几十秒，
--    期间锁表并阻塞采集写入）。线上改注释请先停采集，或直接改本文件。
