-- 加宽滚球赔率的盘口名/选项字段。
--
-- 平台会返回两类超长值，原来的 64/32 会导致 INSERT 报 1406，
-- 整份快照事务回滚、当前快照指针被清空，页面表现为「没有赔率」：
--   market    : '10 Minutes - Gremio FB Porto Alegrense RS Total Corners From 71 To 80' (69)
--   selection : 'Noriega Loret de Mola, Erick Carlos' (35, 1st Goalscorer)
ALTER TABLE kessbet_odds_live
  MODIFY market    VARCHAR(128) NOT NULL
      COMMENT '归一化盘口名，与 prematch 同口径',
  MODIFY selection VARCHAR(64)  NOT NULL
      COMMENT '选项';

ALTER TABLE kessbet_odds_live_history
  MODIFY market    VARCHAR(128) NOT NULL,
  MODIFY selection VARCHAR(64)  NOT NULL;
