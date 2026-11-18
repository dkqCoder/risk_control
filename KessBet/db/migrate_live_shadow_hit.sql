-- 已有数据库执行一次；新库直接使用 db/schema.sql。
-- 影子候选也要落命中表供复核，但必须与正式发布区分。
ALTER TABLE kessbet_arbitrage_snapshot
    ADD COLUMN is_shadow TINYINT NOT NULL DEFAULT 0
        COMMENT '1=影子模式候选，未正式发布；0=正式命中/赛前命中';
