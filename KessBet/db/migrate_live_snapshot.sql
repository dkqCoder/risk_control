-- 已有数据库执行一次；新库直接使用 db/schema.sql，无需再执行本文件。
--
-- D01：把「同一次请求的原始响应 + 解析输入 + 请求前后状态」绑定成一个
-- 不可变快照。分析只按 snapshot_id 读取，不再按时间猜「最近的 raw」。
CREATE TABLE IF NOT EXISTS kessbet_live_snapshot (
    snapshot_id        VARCHAR(40)  NOT NULL,
    platform           VARCHAR(16)  NOT NULL DEFAULT 'kessbet',
    match_id           VARCHAR(32)  NOT NULL,
    generation         BIGINT       NOT NULL,
    request_started_at DATETIME     DEFAULT NULL,
    response_received_at DATETIME   NOT NULL
        COMMENT '本地收到赔率响应的时刻；分析的新鲜度基准',
    state_before       MEDIUMTEXT   DEFAULT NULL
        COMMENT '本次请求前的比赛状态 JSON',
    state_after        MEDIUMTEXT   NOT NULL
        COMMENT '本次请求后的比赛状态 JSON',
    minute             SMALLINT     DEFAULT NULL,
    home_score         SMALLINT     DEFAULT NULL,
    away_score         SMALLINT     DEFAULT NULL,
    raw_payload        MEDIUMTEXT   NOT NULL
        COMMENT '同一次请求的原始 mari 响应 JSON',
    rows_json          MEDIUMTEXT   NOT NULL
        COMMENT '过滤后参与写入/分析的 rows JSON',
    filter_policy      VARCHAR(64)  NOT NULL DEFAULT '',
    created_time       DATETIME     NOT NULL,
    analyzed_time      DATETIME     DEFAULT NULL,
    PRIMARY KEY (snapshot_id),
    KEY idx_match_time (platform, match_id, response_received_at),
    KEY idx_match_gen (platform, match_id, generation)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='滚球不可变输入快照：raw + rows + state 精确绑定';

ALTER TABLE kessbet_match_live
    ADD COLUMN current_snapshot_id VARCHAR(40) DEFAULT NULL
        COMMENT '当前有效输入快照；NULL 表示迁移前/尚未采集',
    ADD COLUMN generation BIGINT NOT NULL DEFAULT 0
        COMMENT '每场递增，阻止旧分析覆盖新结果';

ALTER TABLE kessbet_live_result_cost
    ADD COLUMN snapshot_id VARCHAR(40) DEFAULT NULL
        COMMENT '本次判定使用的输入快照',
    ADD COLUMN generation BIGINT NOT NULL DEFAULT 0
        COMMENT '输入快照的 generation',
    ADD COLUMN verification_status VARCHAR(32) DEFAULT NULL
        COMMENT 'verified / counterexample_found / unsupported_rules / incomplete_model',
    ADD COLUMN verification_detail MEDIUMTEXT DEFAULT NULL
        COMMENT '验证器结果：反例比分、各腿返还等';
