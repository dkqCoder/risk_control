-- 已有数据库执行一次；新库直接使用 db/schema.sql。
-- Task 6：正式命中关联 E1 证据包（文件系统）和 evidence 状态。
ALTER TABLE kessbet_arbitrage_snapshot
    ADD COLUMN evidence_id VARCHAR(40) DEFAULT NULL,
    ADD COLUMN evidence_status VARCHAR(16) DEFAULT NULL
        COMMENT 'pending/complete/failed',
    ADD COLUMN evidence_path VARCHAR(255) DEFAULT NULL,
    ADD COLUMN manifest_hash CHAR(64) DEFAULT NULL,
    ADD COLUMN analysis_id VARCHAR(40) DEFAULT NULL;
