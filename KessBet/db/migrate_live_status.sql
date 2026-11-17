-- 已有数据库执行一次；新库直接使用 db/schema.sql，无需再执行本文件。
--
-- 审核修复：
-- 1) 快照冻结半场比分，避免周期分析另读可变历史（输入一致性）。
-- 2) 结果表允许“无结果”状态（cost NULL + analysis_status），
--    新快照算不出覆盖时也能覆盖旧机会，而不是继续显示旧成本。
ALTER TABLE kessbet_live_snapshot
    ADD COLUMN ht_home SMALLINT DEFAULT NULL,
    ADD COLUMN ht_away SMALLINT DEFAULT NULL;

ALTER TABLE kessbet_live_result_cost
    MODIFY COLUMN cost DOUBLE DEFAULT NULL,
    ADD COLUMN analysis_status VARCHAR(32) DEFAULT NULL
        COMMENT 'valid / no_cover / no_data / incomplete_model / invalidated';
