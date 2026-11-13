-- 已有数据库执行一次；新库直接使用 db/schema.sql，无需再执行本文件。
ALTER TABLE kessbet_arbitrage_snapshot
    ADD COLUMN replay_context MEDIUMTEXT NULL AFTER market_detail
    COMMENT '滚球命中的可重放证据包；赛前命中为空';
