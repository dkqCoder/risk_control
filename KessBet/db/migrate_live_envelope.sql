-- 已有数据库执行一次；新库直接使用 db/schema.sql。
-- Task 2：快照信封、输入指纹、解析/阶段版本。
ALTER TABLE kessbet_live_snapshot
    ADD COLUMN request_json MEDIUMTEXT DEFAULT NULL
        COMMENT '平台请求信封：URL/action/body（脱敏）',
    ADD COLUMN response_json MEDIUMTEXT DEFAULT NULL
        COMMENT '平台响应信封：状态码/必要响应头/原始响应文本/分类',
    ADD COLUMN response_hash CHAR(64) DEFAULT NULL
        COMMENT '实际收到响应文本/字节的 SHA-256',
    ADD COLUMN parser_version VARCHAR(32) DEFAULT NULL,
    ADD COLUMN input_fingerprint CHAR(64) DEFAULT NULL
        COMMENT '规范输入+比分+阶段+策略版本的 SHA-256；不含接收时间',
    ADD COLUMN phase VARCHAR(16) DEFAULT NULL,
    ADD COLUMN phase_source VARCHAR(16) DEFAULT NULL,
    ADD COLUMN ht_source VARCHAR(16) DEFAULT NULL;

ALTER TABLE kessbet_live_result_cost
    ADD COLUMN input_fingerprint CHAR(64) DEFAULT NULL
        COMMENT '本次分析输入指纹；用于命中幂等和重复检测';

ALTER TABLE kessbet_arbitrage_snapshot
    ADD COLUMN input_fingerprint CHAR(64) DEFAULT NULL
        COMMENT '输入指纹；同一输入不重复写正式命中',
    ADD KEY idx_input_fp (platform, match_id, input_fingerprint);
