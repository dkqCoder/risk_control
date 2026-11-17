-- 已有数据库执行一次；新库直接使用 db/schema.sql，无需再执行本文件。
--
-- 滚球赔率的原始响应存档。命中时 analyzer 会把对应版本整份复制进
-- kessbet_arbitrage_snapshot.replay_context.raw，永久保留。
CREATE TABLE IF NOT EXISTS kessbet_odds_live_raw (
    id           BIGINT      NOT NULL AUTO_INCREMENT,
    platform     VARCHAR(16) NOT NULL DEFAULT 'kessbet',
    match_id     VARCHAR(32) NOT NULL,
    fetched_time DATETIME    NOT NULL COMMENT '本地收到响应的时刻（北京时间）',
    payload      MEDIUMTEXT  NOT NULL COMMENT '原始响应 JSON：state_before/after + markets[].mari',
    PRIMARY KEY (id),
    KEY idx_match_time (platform, match_id, fetched_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='滚球赔率原始响应：保留锁定项和完整盘面，仅用于取证';
