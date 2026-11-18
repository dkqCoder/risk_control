CREATE TABLE IF NOT EXISTS kessbet_live_scope_analysis (
    snapshot_id VARCHAR(40) NOT NULL,
    platform VARCHAR(16) NOT NULL DEFAULT 'kessbet',
    match_id VARCHAR(32) NOT NULL,
    scope_key VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    reason VARCHAR(64) DEFAULT NULL,
    result_json MEDIUMTEXT NOT NULL,
    created_time DATETIME NOT NULL,
    PRIMARY KEY (snapshot_id, scope_key),
    KEY idx_match_time (platform, match_id, created_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
