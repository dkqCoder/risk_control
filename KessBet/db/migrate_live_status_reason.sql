-- 已有数据库执行一次；新库直接使用 db/schema.sql。
-- Task 5：当前状态原因，供 API/页面解释 no_candidate / unverifiable / invalid_input。
ALTER TABLE kessbet_live_result_cost
    ADD COLUMN status_reason VARCHAR(64) DEFAULT NULL
        COMMENT 'no_cover/no_data/no_score/incomplete_model/verified/counterexample_found/...';
