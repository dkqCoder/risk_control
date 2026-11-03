"""db/dao.py — 数据库访问层。

**线程安全**：每个线程持有独立的连接（threading.local）。
pymysql.Connection 不是线程安全的，多线程共享会导致连接错乱。
"""
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

import pymysql

# 北京时间（UTC+8，无夏令时）。库里所有时间都存这个时区的 naive 值。
BEIJING = timezone(timedelta(hours=8))

log = logging.getLogger(__name__)

# 连接参数全部来自环境变量（部署时由 deploy/.env 注入）。
# 不要在这里写默认密码——本仓库会推送到远端，明文凭据一旦进 git 历史
# 就难以彻底清除。
DEFAULT_DSN = dict(
    host=os.environ.get('KESSBET_DB_HOST', '127.0.0.1'),
    port=int(os.environ.get('KESSBET_DB_PORT', '3306')),
    database=os.environ.get('KESSBET_DB_NAME', 'sssj'),
    user=os.environ.get('KESSBET_DB_USER', 'kessbet'),
    password=os.environ.get('KESSBET_DB_PASSWORD', ''),
)


def beijing_now() -> datetime:
    """当前 naive 北京时间。"""
    return datetime.now(timezone.utc).astimezone(BEIJING).replace(tzinfo=None)


class Dao:
    """线程安全 DAO：每线程一条连接。

    用法：
        dao = Dao()               # 主线程连接
        dao.conn                  # 当前线程的连接
        dao.close()               # 只关当前线程的连接
    """

    def __init__(self, **dsn):
        self._dsn = dict(DEFAULT_DSN)
        self._dsn.update(dsn)
        self._dsn.setdefault('connect_timeout', 8)
        self._dsn.setdefault('charset', 'utf8mb4')
        self._dsn.setdefault('autocommit', False)
        # 密码只从环境变量来。缺了就立刻报错，不要等到 pymysql 抛
        # "Access denied" 才发现是没加载 .env。
        if not self._dsn.get('password'):
            raise RuntimeError(
                '数据库密码未设置。请先载入配置：\n'
                '    set -a; . deploy/.env; set +a\n'
                '或设置环境变量 KESSBET_DB_PASSWORD')
        self._local = threading.local()
        # 连接代数：reset_connections() 递增，各线程发现代数变了就重建连接
        self._epoch = 0
        self._epoch_lock = threading.Lock()

    # ---------- 连接管理 ----------

    @property
    def conn(self):
        """当前线程的连接。不存在、已断开、或代数过期则新建。"""
        c = getattr(self._local, 'conn', None)

        # 代数不匹配 -> 说明别处调用过 reset_connections()，丢弃旧连接
        if c is not None and getattr(self._local, 'epoch', -1) != self._epoch:
            try:
                c.close()
            except Exception:
                pass
            c = None

        if c is not None:
            try:
                c.ping(reconnect=False)     # 只检测，不自动重连
            except Exception:
                try:
                    c.close()
                except Exception:
                    pass
                c = None
        if c is None:
            c = pymysql.connect(**self._dsn)
            self._local.conn = c
            self._local.epoch = self._epoch
        return c

    def close(self):
        """关闭当前线程的连接。"""
        c = getattr(self._local, 'conn', None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            self._local.conn = None

    def close_all(self):
        """仅供主线程调用——其他线程的连接会随线程结束自动回收。"""
        self.close()

    def reset_connections(self) -> None:
        """标记所有线程连接失效，下次访问时重建。

        用于采集连续失败后的自愈：可能只是连接池坏了（WinError 10013
        之类的瞬时套接字错误），重建后能恢复。

        threading.local 无法遍历其他线程的副本，所以用一个全局代数计数器：
        每个线程记录自己上次用的代数，代数不匹配就丢弃旧连接。
        """
        with self._epoch_lock:
            self._epoch += 1
        # 当前线程也立刻断掉
        self.close()

    # ---------- match ----------

    def upsert_matches(self, matches) -> int:
        if not matches:
            return 0
        now = beijing_now()
        rows = [(
            m.platform, m.match_id, m.sport, m.sportradar_id,
            m.league, m.country, m.home_team, m.away_team,
            m.kickoff_time.astimezone(BEIJING).replace(tzinfo=None) if m.kickoff_time.tzinfo else m.kickoff_time,
            now, now,
        ) for m in matches]

        sql = """
        INSERT INTO kessbet_match
          (platform, match_id, sport, sportradar_id, league, country,
           home_team, away_team, kickoff_time, first_seen_time, updated_time)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
          sport=VALUES(sport), sportradar_id=VALUES(sportradar_id),
          league=VALUES(league), country=VALUES(country),
          home_team=VALUES(home_team), away_team=VALUES(away_team),
          kickoff_time=VALUES(kickoff_time), updated_time=VALUES(updated_time)
        """
        with self.conn.cursor() as cur:
            cur.executemany(sql, rows)
        self.conn.commit()
        return len(rows)

    def get_match(self, match_id: str):
        """返回 (sport, home_team, away_team, kickoff_time) 或 None。"""
        sql = """
        SELECT sport, home_team, away_team, kickoff_time
        FROM kessbet_match WHERE platform=%s AND match_id=%s
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, ('kessbet', match_id))
            return cur.fetchone()

    def upcoming(self, hours: int) -> list[tuple]:
        """未来 N 小时内开赛的比赛 (match_id, sport, home, away, kickoff)。"""
        sql = """
        SELECT match_id, sport, home_team, away_team, kickoff_time
        FROM kessbet_match
        WHERE platform='kessbet'
          AND kickoff_time BETWEEN NOW() AND NOW() + INTERVAL %s HOUR
        ORDER BY kickoff_time
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (hours,))
            return list(cur.fetchall())

    # ---------- 跨平台映射 ----------
    #
    # 页面和跨平台比价都走 canonical_id；单平台采集/分析仍用 (platform, match_id)，
    # 不受这张表影响。

    @staticmethod
    def canonical_key(platform: str, match_id: str, sportradar_id: str) -> str:
        """算出一场比赛的统一键。

        优先用 sportradar_id —— 它是行业标准，跨平台一致。
        没有时退回平台内唯一键，保证仍然可聚合（只是无法和其他平台对齐）。
        """
        if sportradar_id:
            return sportradar_id
        return f'local:{platform}:{match_id}'

    def sync_canonical(self, matches) -> int:
        """把赛程同步进跨平台映射表。每次 upsert_matches 之后调一次。"""
        if not matches:
            return 0
        now = beijing_now()
        rows = []
        for m in matches:
            kickoff = (m.kickoff_time.astimezone(BEIJING).replace(tzinfo=None)
                       if m.kickoff_time.tzinfo else m.kickoff_time)
            rows.append((
                self.canonical_key(m.platform, m.match_id, m.sportradar_id),
                m.platform, m.match_id, m.sport,
                'sportradar' if m.sportradar_id else 'fallback',
                m.home_team, m.away_team, kickoff, now,
            ))
        sql = """
        INSERT INTO kessbet_match_canonical
          (canonical_id, platform, match_id, sport, match_source,
           home_team, away_team, kickoff_time, updated_time)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
          canonical_id=VALUES(canonical_id), sport=VALUES(sport),
          match_source=VALUES(match_source),
          home_team=VALUES(home_team), away_team=VALUES(away_team),
          kickoff_time=VALUES(kickoff_time), updated_time=VALUES(updated_time)
        """
        with self.conn.cursor() as cur:
            cur.executemany(sql, rows)
        self.conn.commit()
        return len(rows)

    def platforms_for(self, canonical_id: str) -> list[str]:
        """该场比赛在哪些平台有报价。"""
        sql = ("SELECT DISTINCT platform FROM kessbet_match_canonical "
               "WHERE canonical_id=%s ORDER BY platform")
        with self.conn.cursor() as cur:
            cur.execute(sql, (canonical_id,))
            return [r[0] for r in cur.fetchall()]

    def get_canonical_match(self, canonical_id: str):
        """跨平台聚合后的比赛信息。

        多个平台对同一场比赛的队名/联赛写法可能不同，这里取开赛最晚的一条
        作为展示用（通常是数据更完整的那个），并统计平台数。
        返回 (canonical_id, home, away, kickoff, sport, platforms, n_platforms) 或 None。
        """
        sql = """
        SELECT canonical_id, home_team, away_team, kickoff_time, sport,
               GROUP_CONCAT(DISTINCT platform ORDER BY platform) AS plats,
               COUNT(DISTINCT platform) AS n
        FROM kessbet_match_canonical
        WHERE canonical_id=%s
        GROUP BY canonical_id, home_team, away_team, kickoff_time, sport
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (canonical_id,))
            return cur.fetchone()

    def get_selections_by_platform(self, canonical_id: str, platform: str) -> list[tuple]:
        """读某平台在某场比赛上的全部选项，返回和 get_selections 一样的结构。"""
        sql = """
        SELECT o.market, o.side, o.line, o.selection, o.odds
        FROM kessbet_odds_prematch o
        JOIN kessbet_match_canonical c
          ON c.platform = o.platform AND c.match_id = o.match_id
        WHERE c.canonical_id=%s AND o.platform=%s
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (canonical_id, platform))
            return [(r[0], r[1], r[2], r[3], float(r[4])) for r in cur.fetchall()]

    def merged_selections(self, canonical_id: str) -> list[tuple]:
        """跨平台合并：同一选项取所有平台里的最高赔率。

        返回 (market, side, line, selection, odds, platform)，只保留最优报价。
        这是跨平台套利的正确输入 —— 每个选项在哪家买最划算。
        """
        sql = """
        SELECT o.market, o.side, o.line, o.selection,
               MAX(o.odds) AS best_odds,
               SUBSTRING_INDEX(GROUP_CONCAT(o.platform ORDER BY o.odds DESC), ',', 1)
                   AS best_platform
        FROM kessbet_odds_prematch o
        JOIN kessbet_match_canonical c
          ON c.platform = o.platform AND c.match_id = o.match_id
        WHERE c.canonical_id=%s
        GROUP BY o.market, o.side, o.line, o.selection
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (canonical_id,))
            return [(r[0], r[1], r[2], r[3], float(r[4]), r[5])
                    for r in cur.fetchall()]

    # ---------- odds ----------

    def count_odds(self, match_id: str) -> int:
        sql = "SELECT COUNT(*) FROM kessbet_odds_prematch WHERE platform='kessbet' AND match_id=%s"
        with self.conn.cursor() as cur:
            cur.execute(sql, (match_id,))
            return cur.fetchone()[0]

    def fetch_keys(self, match_id: str) -> set[tuple]:
        sql = ("SELECT market, side, line, selection FROM kessbet_odds_prematch "
               "WHERE platform='kessbet' AND match_id=%s")
        with self.conn.cursor() as cur:
            cur.execute(sql, (match_id,))
            return {(r[0], r[1], r[2], r[3]) for r in cur.fetchall()}

    def get_selections(self, match_id: str) -> list[tuple]:
        sql = """
        SELECT market, side, line, selection, odds
        FROM kessbet_odds_prematch
        WHERE platform='kessbet' AND match_id=%s
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (match_id,))
            return [(r[0], r[1], r[2], r[3], float(r[4])) for r in cur.fetchall()]

    def delete_keys(self, match_id: str, keys: set[tuple]) -> int:
        if not keys:
            return 0
        sql = """
        DELETE FROM kessbet_odds_prematch
        WHERE platform=%s AND match_id=%s
          AND market=%s AND side=%s AND line=%s AND selection=%s
        """
        rows = [('kessbet', match_id, k[0], k[1], k[2], k[3]) for k in keys]
        with self.conn.cursor() as cur:
            cur.executemany(sql, rows)
            return cur.rowcount

    def write_hit(self, match_id: str, sport: str, cost: float,
                  markets_json: str, kickoff_time) -> None:
        sql = """
        INSERT INTO kessbet_odds_hit
          (alert_time, platform, match_id, sport, cost, markets, kickoff_time)
        VALUES (NOW(), 'kessbet', %s, %s, %s, %s, %s)
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (match_id, sport, cost, markets_json, kickoff_time))
        self.conn.commit()

    # ---------- log ----------

    def log_event(self, event: str, started: datetime, matches: int = 0,
                  rows: int = 0, error: str | None = None) -> None:
        sql = """
        INSERT INTO kessbet_collect_log
          (platform, event, started_time, finished_time, matches_count, rows_written, error)
        VALUES ('kessbet', %s, %s, NOW(), %s, %s, %s)
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, (event, started, matches, rows, error))
            self.conn.commit()
        except Exception as e:
            log.warning('log_event failed: %s', e)

    # ---------- 清理 ----------

    def cleanup(self, history_days: int = 7,
                prematch_days: int = 7,
                match_days: int = 7) -> dict:
        """清理过期数据。三张表统一保留 7 天（按开赛时间）。

        - odds_prematch: 保留 N 天
        - odds_history:  保留 N 天
        - match:         保留 N 天

        按 kickoff_time 而不是写入时间清理——一场比赛的所有数据
        同时进来、同时过期，避免"当前状态在但历史没了"这类不一致。

        注意：prematch 保留 7 天意味着已结束比赛的数据也在表里，
        查询时如果要"只看未开赛"必须显式加 kickoff_time > NOW()。
        """
        result = {}
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM kessbet_odds_prematch "
                "WHERE kickoff_time < NOW() - INTERVAL %s DAY",
                (prematch_days,))
            result['prematch'] = cur.rowcount

            cur.execute(
                "DELETE FROM kessbet_odds_history "
                "WHERE kickoff_time < NOW() - INTERVAL %s DAY",
                (history_days,))
            result['history'] = cur.rowcount

            cur.execute(
                "DELETE FROM kessbet_match "
                "WHERE kickoff_time < NOW() - INTERVAL %s DAY",
                (match_days,))
            result['match'] = cur.rowcount

            cur.execute(
                "DELETE FROM kessbet_collect_log "
                "WHERE started_time < NOW() - INTERVAL 30 DAY")
            result['log'] = cur.rowcount
        self.conn.commit()
        return result
