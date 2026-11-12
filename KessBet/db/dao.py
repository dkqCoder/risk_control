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
        # 用 READ COMMITTED 而不是 MySQL 默认的 REPEATABLE READ。
        #
        # 原因：REPEATABLE READ 下，一条连接读到第一行时就固定了整场事务的
        # 一致性快照，之后不 commit 就一直看不到别处的写入。本项目的读取方
        # 都是长期存活的只读循环（analyze.run 每轮只读、从不 commit；
        # collect.run 主线程只做查询），所以会永久停在启动那一刻的数据上：
        # 实测 analyze_once 第二轮起看到的永远是第一轮的赔率，report()
        # 报 prematch=0 而库里实际有 5 万多行。
        #
        # READ COMMITTED 每条语句重新取快照，读循环天然看到最新数据。
        # 写入侧不受影响：writer/dao 的写操作都是单场事务 + 显式 commit，
        # 不依赖 REPEATABLE READ 的“事务内可重复读”语义。
        self._dsn.setdefault('init_command',
                             'SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
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

    def known_match_ids(self, platform: str = 'kessbet') -> set[str]:
        """库里已有的全部 match_id。

        给「滚球列表补采」用：拉回来的滚球场次里，哪些是我们还不知道的。
        只取一列，4158 场约 100KB，比逐场查便宜得多。
        """
        with self.conn.cursor() as cur:
            cur.execute("SELECT match_id FROM kessbet_match WHERE platform=%s",
                        (platform,))
            return {r[0] for r in cur.fetchall()}

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

    # ---------- 分析结果快照 ----------
    #
    # analyze 每轮把每场的判定结果写进来，页面直接读。
    # 页面**绝不能**在请求里跑 ILP —— 单场 1.2 秒，一屏 50 场就是 60 秒。

    def write_cost(self, platform: str, match_id: str, sport: str,
                   kickoff_time, cost, cost_scope: str,
                   n_markets: int, n_selections: int, n_candidates: int,
                   ilp_chosen: str | None, lp_worst_profit,
                   lp_chosen: str | None, cross_gap, error: str | None,
                   odds_updated, computed_time) -> None:
        sql = """
        INSERT INTO kessbet_match_cost
          (platform, match_id, sport, kickoff_time, cost, cost_scope,
           n_markets, n_selections, n_candidates, ilp_chosen,
           lp_worst_profit, lp_chosen, cross_gap, error,
           odds_updated, computed_time)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
          sport=VALUES(sport), kickoff_time=VALUES(kickoff_time),
          cost=VALUES(cost), cost_scope=VALUES(cost_scope),
          n_markets=VALUES(n_markets), n_selections=VALUES(n_selections),
          n_candidates=VALUES(n_candidates), ilp_chosen=VALUES(ilp_chosen),
          lp_worst_profit=VALUES(lp_worst_profit), lp_chosen=VALUES(lp_chosen),
          cross_gap=VALUES(cross_gap), error=VALUES(error),
          odds_updated=VALUES(odds_updated), computed_time=VALUES(computed_time)
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (
                platform, match_id, sport, kickoff_time, cost, cost_scope,
                n_markets, n_selections, n_candidates, ilp_chosen,
                lp_worst_profit, lp_chosen, cross_gap, error,
                odds_updated, computed_time,
            ))

    def cost_updated_at(self, match_id: str, platform: str = 'kessbet'):
        """该场赔率最后一次变更时间，用来判断能否跳过重算。

        取 odds_prematch 的 MAX(updated_time)，比读整场选项便宜得多。
        """
        sql = """SELECT MAX(updated_time) FROM kessbet_odds_prematch
                 WHERE platform=%s AND match_id=%s"""
        with self.conn.cursor() as cur:
            cur.execute(sql, (platform, match_id))
            return cur.fetchone()[0]

    # ---------- 成本变动流水 ----------

    def append_cost_history(self, platform: str, match_id: str, cost: float,
                            prev_cost, odds_updated, changed_time) -> bool:
        """成本变化时追加一行。返回 True 表示确实写了新行。

        只在成本真的变了才写 —— analyze 每 2 秒跑一轮，无条件写会让
        一场比赛一天产出几万行，而其中绝大多数成本一分没动。
        唯一键 (platform, match_id, odds_updated) 兜底：同一份赔率重算
        两次（比如进程重启后补算）不会产生重复行。
        """
        if cost is None:
            return False
        sql = """
        INSERT INTO kessbet_match_cost_history
          (platform, match_id, cost, prev_cost, odds_updated, changed_time)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE id = id
        """
        with self.conn.cursor() as cur:
            n = cur.execute(sql, (platform, match_id, cost, prev_cost,
                                  odds_updated, changed_time))
        # 不在这里 commit：由调用方和 write_cost 一起提交，
        # 避免两条记录落在不同事务里导致「成本表更新了但流水没跟上」
        return n == 1

    def cost_at(self, match_ids: list[str], minutes: int = 60,
                platform: str = 'kessbet') -> dict[str, dict]:
        """批量取每场「N 分钟前」的成本，供列表页算 1h 变化。

        取的是 changed_time <= 截止时刻的**最后一行**。注意这是
        「截止时刻时的成本」，不是「N 分钟内的最小/最大」—— 后者需要
        扫描窗口内所有行，量级完全不同。

        没有历史行的比赛不出现在结果里，前端据此显示「—」，
        而不是拿当前值充数（那会让每场都显示 0）。
        """
        if not match_ids:
            return {}
        ph = ','.join(['%s'] * len(match_ids))
        sql = f"""
        SELECT h.match_id, h.cost
        FROM kessbet_match_cost_history h
        JOIN (
            SELECT match_id, MAX(changed_time) AS mt
            FROM kessbet_match_cost_history
            WHERE platform=%s AND match_id IN ({ph})
              AND changed_time <= NOW() - INTERVAL %s MINUTE
            GROUP BY match_id
        ) x ON x.match_id = h.match_id AND x.mt = h.changed_time
        WHERE h.platform=%s
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, [platform] + list(match_ids) + [minutes, platform])
            return {r[0]: {'cost': float(r[1])} for r in cur.fetchall()}

    def cleanup_cost_history(self, days: int = 7) -> int:
        """按时间清理。这张表只增不改，不清理会一直涨。"""
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM kessbet_match_cost_history "
                        "WHERE changed_time < NOW() - INTERVAL %s DAY", (days,))
            n = cur.rowcount
        self.conn.commit()
        return n

    # ---------- 滚球数据 ----------

    def live_matches(self, window_min: int = 150) -> list[dict]:
        """当前处于滚球窗口内的比赛（已开赛且在窗口内）。

        为什么按 kickoff_time 而不是 status：
          status 存在 kessbet_match_live 里，但那是「采过之后」才有的。
          第一次采的时候还没记录，得靠 kickoff_time 判断该不该采。
        """
        sql = """
        SELECT match_id, sport, home_team, away_team, kickoff_time
        FROM kessbet_match
        WHERE platform='kessbet'
          AND kickoff_time <= NOW()
          AND kickoff_time > NOW() - INTERVAL %s MINUTE
        ORDER BY kickoff_time
        """
        with self.conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, (window_min,))
            return list(cur.fetchall())

    def save_live_state(self, match_id: str, state: dict, kickoff_time,
                        platform: str = 'kessbet') -> None:
        """写比赛实时状态（一场一行，覆盖）。

        state 是 ACTION=0014 的原始响应。字段都是字符串，
        这里统一转 int —— 存成字符串会让后续比较和计算变麻烦。
        """
        def _i(v):
            if v in (None, '', '-'):
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        mt = state.get('mt') or ''
        minute = None
        if ':' in str(mt):
            try:
                minute = int(str(mt).split(':')[0])
            except (ValueError, IndexError):
                pass

        sql = """
        INSERT INTO kessbet_match_live
          (platform, match_id, status, minute,
           home_score, away_score, home_corner, away_corner,
           home_yellow, away_yellow, home_red, away_red,
           kickoff_time, fetched_time)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON DUPLICATE KEY UPDATE
          status=VALUES(status), minute=VALUES(minute),
          home_score=VALUES(home_score), away_score=VALUES(away_score),
          home_corner=VALUES(home_corner), away_corner=VALUES(away_corner),
          home_yellow=VALUES(home_yellow), away_yellow=VALUES(away_yellow),
          home_red=VALUES(home_red), away_red=VALUES(away_red),
          kickoff_time=VALUES(kickoff_time), fetched_time=NOW()
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (
                platform, match_id, _i(state.get('ms')), minute,
                _i(state.get('hmp')), _i(state.get('amp')),
                _i(state.get('hck')), _i(state.get('ack')),
                _i(state.get('hyc')), _i(state.get('ayc')),
                _i(state.get('hrc')), _i(state.get('arc')),
                kickoff_time,
            ))
        self.conn.commit()

    def live_analyze_pending(self, window_min: int = 150,
                             platform: str = 'kessbet') -> list[dict]:
        """赔率有更新的滚球比赛。类似 prematch 的 pending_matches，
        但读的是 live 表。

        只返回「赔率比上次计算新」的场次 —— 赔率没变重算没有意义。
        """
        sql = """
        SELECT m.match_id, m.sport, m.home_team, m.away_team,
               m.kickoff_time, l.minute, l.status,
               l.home_score, l.away_score,
               (SELECT MAX(o.updated_time) FROM kessbet_odds_live o
                WHERE o.platform=m.platform AND o.match_id=m.match_id)
                   AS odds_updated
        FROM kessbet_match m
        LEFT JOIN kessbet_match_live l
          ON l.platform=m.platform AND l.match_id=m.match_id
        WHERE m.platform=%s
          AND m.sport='sr:sport:1'
          AND m.kickoff_time <= NOW()
          AND m.kickoff_time > NOW() - INTERVAL %s MINUTE
          AND EXISTS (SELECT 1 FROM kessbet_odds_live o
                      WHERE o.platform=m.platform AND o.match_id=m.match_id)
          AND NOT EXISTS (
              SELECT 1 FROM kessbet_live_result_cost c
              WHERE c.platform=m.platform AND c.match_id=m.match_id
                AND c.computed_time >= (
                  SELECT MAX(o2.updated_time)
                  FROM kessbet_odds_live o2
                  WHERE o2.platform=m.platform AND o2.match_id=m.match_id))
        ORDER BY m.kickoff_time
        """
        with self.conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, (platform, window_min))
            return list(cur.fetchall())

    def live_analyze_targets(self, window_min: int = 150,
                             platform: str = 'kessbet') -> list[dict]:
        """需要跑滚球套利判定的比赛：窗口内 + 有滚球赔率。

        和不带条件的 live_matches 的区别：
          - 要求 kessbet_odds_live 里确实有数据（没采到的场次判也没用）
          - 带上 minute / 总分，判定时要用分钟挡掉过期的窗口盘

        minute 取 kessbet_match_live 的当前值，没有就 NULL。
        缺失不跳过 —— 没分钟只是不能挡过期窗口，不是不能判定。
        """
        sql = """
        SELECT m.match_id, m.sport, m.home_team, m.away_team,
               m.kickoff_time, l.minute, l.status,
               l.home_score, l.away_score,
               (SELECT MAX(o.updated_time) FROM kessbet_odds_live o
                WHERE o.platform=m.platform AND o.match_id=m.match_id)
                   AS odds_updated
        FROM kessbet_match m
        LEFT JOIN kessbet_match_live l
          ON l.platform=m.platform AND l.match_id=m.match_id
        WHERE m.platform=%s
          AND m.sport='sr:sport:1'
          AND m.kickoff_time <= NOW()
          AND m.kickoff_time > NOW() - INTERVAL %s MINUTE
          AND EXISTS (SELECT 1 FROM kessbet_odds_live o
                      WHERE o.platform=m.platform AND o.match_id=m.match_id)
        ORDER BY m.kickoff_time
        """
        with self.conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, (platform, window_min))
            return list(cur.fetchall())

    def live_odds_updated_at(self, match_id: str,
                             platform: str = 'kessbet'):
        """滚球赔率最后一次变更时间，用于跳过没变的场次。"""
        sql = """SELECT MAX(updated_time) FROM kessbet_odds_live
                 WHERE platform=%s AND match_id=%s"""
        with self.conn.cursor() as cur:
            cur.execute(sql, (platform, match_id))
            return cur.fetchone()[0]

    def get_live_selections(self, match_id: str,
                            platform: str = 'kessbet') -> list[tuple]:
        """读一场比赛的滚球选项，结构和 get_selections 一致。"""
        sql = """
        SELECT market, side, line, selection, odds
        FROM kessbet_odds_live
        WHERE platform=%s AND match_id=%s
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (platform, match_id))
            return [(r[0], r[1], r[2], r[3], float(r[4])) for r in cur.fetchall()]

    def live_history_for_half_time(self, match_id: str,
                                   platform: str = 'kessbet') -> list[tuple]:
        """某场比赛赔率流水里的 (minute, home_score, away_score)。

        给「反推半场比分」用。只取需要判断半场的行（minute <= 45），
        避免把整场几万行都读出来。

        为什么不用 fetch_state 的 msScoreList：
          实测当前比分 1:0 时它返回两个 0:0 的阶段快照，
          那是「每个阶段的比分变化记录」，不是半场比分。
        """
        sql = """
        SELECT minute, home_score, away_score
        FROM kessbet_odds_live_history
        WHERE platform=%s AND match_id=%s
          AND minute IS NOT NULL AND minute <= 45
          AND home_score IS NOT NULL AND away_score IS NOT NULL
        GROUP BY minute, home_score, away_score
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (platform, match_id))
            return [(r[0], r[1], r[2]) for r in cur.fetchall()]

    def write_live_result_cost(self, match_id: str, kickoff_time, cost: float,
                               profit_pct, chosen: str, reach_states: int,
                               n_candidates: int, minute, home_score,
                               away_score, ht_home, ht_away,
                               odds_updated, computed_time,
                               platform: str = 'kessbet') -> None:
        """写一场比赛的「比赛结果」套利判定（一场一行，覆盖）。

        和窗口成本不同，这个**不删旧行** —— 一场比赛只有一个结果判定，
        UPSERT 就够了。窗口盘是一场多行、会随时间增删，才需要先删后插。
        """
        sql = """
        INSERT INTO kessbet_live_result_cost
          (platform, match_id, cost, profit_pct, chosen, reach_states,
           n_candidates, minute, home_score, away_score, ht_home, ht_away,
           kickoff_time, odds_updated, computed_time)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
          cost=VALUES(cost), profit_pct=VALUES(profit_pct),
          chosen=VALUES(chosen), reach_states=VALUES(reach_states),
          n_candidates=VALUES(n_candidates), minute=VALUES(minute),
          home_score=VALUES(home_score), away_score=VALUES(away_score),
          ht_home=VALUES(ht_home), ht_away=VALUES(ht_away),
          kickoff_time=VALUES(kickoff_time),
          odds_updated=VALUES(odds_updated),
          computed_time=VALUES(computed_time)
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, (
                platform, match_id, cost, profit_pct, chosen, reach_states,
                n_candidates, minute, home_score, away_score, ht_home, ht_away,
                kickoff_time, odds_updated, computed_time,
            ))
        self.conn.commit()

    def get_live_result_costs(self, match_ids: list[str],
                              platform: str = 'kessbet') -> dict[str, dict]:
        """批量读结果成本，一次查询搞定列表页。"""
        if not match_ids:
            return {}
        ph = ','.join(['%s'] * len(match_ids))
        sql = f"""
        SELECT match_id, cost, profit_pct, chosen, reach_states,
               n_candidates, minute, home_score, away_score,
               ht_home, ht_away, computed_time
        FROM kessbet_live_result_cost
        WHERE platform=%s AND match_id IN ({ph})
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, [platform] + list(match_ids))
            out = {}
            for r in cur.fetchall():
                out[r[0]] = {
                    'cost': float(r[1]) if r[1] is not None else None,
                    'profit_pct': float(r[2]) if r[2] is not None else None,
                    'chosen': r[3], 'reach_states': r[4],
                    'n_candidates': r[5], 'minute': r[6],
                    'home_score': r[7], 'away_score': r[8],
                    'ht_home': r[9], 'ht_away': r[10],
                    'computed_time': r[11],
                }
            return out

    def get_live_result_cost(self, match_id: str,
                             platform: str = 'kessbet'):
        return self.get_live_result_costs([match_id], platform).get(match_id)

    def replace_live_odds(self, match_id: str, kickoff_time, selections,
                          minute=None, home_score=None, away_score=None,
                          platform: str = 'kessbet') -> dict:
        """整场替换滚球赔率：写流水 + upsert 当前值 + 删除接口里没有的行。

        selections: [(market, side, line, selection, odds), ...]

        数据源是 HTTP 全量接口，所以「接口里没有」==「平台上真的没有了」。
        必须删，否则已撤的盘口会一直留在表里：
          - 窗口盘过期后（比如第 85 分钟还留着 `From 61 To 75`），
            拿它算 Σ(1/赔率) 会得到一个无法下注的「套利」
          - 实测 890254 那场表内 353 行、平台只给 170 行，241 个是已撤的

        ⚠️ 与赛前不同，这里**不做**「接口返回少于库内就跳过」的比例保护：
          滚球盘口会大规模上下架，比例保护会误挡正常撤盘。
          但如果接口返回空，应当由调用方跳过（空响应多半是接口故障）。
        """
        if not selections:
            return {'written': 0, 'changed': 0, 'removed': 0}

        # 读当前值，找出真正变化的
        sql_cur = """
        SELECT market, side, line, selection, odds
        FROM kessbet_odds_live
        WHERE platform=%s AND match_id=%s
        """
        with self.conn.cursor() as cur:
            cur.execute(sql_cur, (platform, match_id))
            current = {(r[0], r[1], r[2], r[3]): float(r[4])
                       for r in cur.fetchall()}

        history_rows = []
        for m, s, l, lab, odds in selections:
            key = (m, s, l, lab)
            old = current.get(key)
            if old is None or abs(old - odds) > 1e-9:
                history_rows.append((
                    platform, match_id, m, s, l, lab, odds, old,
                    minute, home_score, away_score, kickoff_time,
                ))

        fresh_keys = {(m, s, l, lab) for m, s, l, lab, _ in selections}
        stale = set(current) - fresh_keys

        try:
            with self.conn.cursor() as cur:
                if history_rows:
                    cur.executemany("""
                    INSERT INTO kessbet_odds_live_history
                      (platform, match_id, market, side, line, selection,
                       odds, prev_odds, minute, home_score, away_score,
                       kickoff_time, changed_time)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    """, history_rows)

                # prev_odds 用 MySQL 的 ON DUPLICATE 语义：
                # 「把旧的 odds 存进 prev_odds，再写新 odds」。
                # 赋值顺序很重要 —— MySQL 按从左到右求值，
                # 如果写成 odds=VALUES(odds), prev_odds=odds，
                # 第二个 odds 已经是新值了。必须先 prev_odds=odds。
                cur.executemany("""
                INSERT INTO kessbet_odds_live
                  (platform, match_id, market, side, line, selection,
                   odds, kickoff_time, updated_time)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON DUPLICATE KEY UPDATE
                  prev_odds=odds, odds=VALUES(odds), updated_time=NOW()
                """, [(
                    platform, match_id, m, s, l, lab, odds, kickoff_time,
                ) for m, s, l, lab, odds in selections])
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        # 删已撤的行。放在 commit 之后单独一步：撤盘日志写失败不该
        # 连带丢掉上面真正的赔率更新（和 writer.replace_match 同样的取舍）。
        removed = 0
        if stale:
            try:
                self.log_removed(
                    match_id,
                    [(m, s, l, lab, current[(m, s, l, lab)])
                     for m, s, l, lab in stale],
                    removed_time=beijing_now(), source='live',
                    platform=platform,
                )
            except Exception as e:
                log.warning('log_removed(live) %s failed: %s', match_id, e)
            with self.conn.cursor() as cur:
                cur.executemany("""
                DELETE FROM kessbet_odds_live
                WHERE platform=%s AND match_id=%s
                  AND market=%s AND side=%s AND line=%s AND selection=%s
                """, [(platform, match_id, m, s, l, lab)
                      for m, s, l, lab in stale])
                removed = cur.rowcount
            self.conn.commit()

        return {'written': len(selections), 'changed': len(history_rows),
                'removed': removed}

    def mark_live_finished(self, match_id: str,
                           platform: str = 'kessbet') -> None:
        """把比赛标记为已结束（status=100），并清掉它的滚球赔率与判定结果。

        赔率为什么清：比赛结束后接口再也不返回数据，留着只是历史包袱。
        需要历史的话在 kessbet_odds_live_history 里。

        判定结果为什么也要清（踩过的坑，2026-11-12 APIA）：
          18:20 算出的结果里选中了 `Total H 4.5`，
          18:21 解析器修复后该盘线被识别为锁定并从赔率表删除。
          比赛结束后没有任何赔率 → 不在 live_analyze_targets 里
          → 再也不会被重算 → 结果表里那条引用了已不存在选项的记录就永远留着。
          页面点进去会看到一个「用锁定盘口算出来的套利」。
        """
        with self.conn.cursor() as cur:
            cur.execute("""UPDATE kessbet_match_live SET status=100
                           WHERE platform=%s AND match_id=%s""",
                        (platform, match_id))
            cur.execute("""DELETE FROM kessbet_odds_live
                           WHERE platform=%s AND match_id=%s""",
                        (platform, match_id))
            cur.execute("""DELETE FROM kessbet_live_result_cost
                           WHERE platform=%s AND match_id=%s""",
                        (platform, match_id))
        self.conn.commit()

    def cleanup_live(self, days: int = 3) -> dict:
        """滚球数据保留期比赛前短 —— 比赛结束后就没有分析价值了。

        四张表都按自己的 kickoff_time 清理，互相独立。
        不要用「子查询 kessbet_match」的方式关联 —— 那张表先被清掉的话，
        子查询查不到任何行，本表的数据就永远留着成孤儿。
        """
        result = {}
        with self.conn.cursor() as cur:
            for table, key in (('kessbet_odds_live', 'live_odds'),
                               ('kessbet_odds_live_history', 'live_history'),
                               ('kessbet_match_live', 'live_state')):
                cur.execute(f"DELETE FROM {table} "
                            f"WHERE kickoff_time < NOW() - INTERVAL %s DAY",
                            (days,))
                result[key] = cur.rowcount
        self.conn.commit()
        return result


    def get_costs(self, match_ids: list[str],
                  platform: str = 'kessbet') -> dict[str, dict]:
        """批量读成本快照，一次查询搞定列表页。"""
        if not match_ids:
            return {}
        ph = ','.join(['%s'] * len(match_ids))
        sql = f"""
        SELECT match_id, cost, cost_scope, n_markets, n_selections,
               n_candidates, ilp_chosen, lp_worst_profit, lp_chosen,
               cross_gap, error, computed_time
        FROM kessbet_match_cost
        WHERE platform=%s AND match_id IN ({ph})
        """
        with self.conn.cursor() as cur:
            cur.execute(sql, [platform] + list(match_ids))
            out = {}
            for r in cur.fetchall():
                out[r[0]] = {
                    'cost': float(r[1]) if r[1] is not None else None,
                    'cost_scope': r[2], 'n_markets': r[3], 'n_selections': r[4],
                    'n_candidates': r[5], 'ilp_chosen': r[6],
                    'lp_worst_profit': float(r[7]) if r[7] is not None else None,
                    'lp_chosen': r[8],
                    'cross_gap': float(r[9]) if r[9] is not None else None,
                    'error': r[10], 'computed_time': r[11],
                }
        return out

    def get_cost(self, match_id: str, platform: str = 'kessbet'):
        """读单场的成本快照。analyze 用来比较「和上一轮比变没变」。"""
        sql = """SELECT cost, cost_scope, n_markets, n_selections,
                        n_candidates, ilp_chosen, lp_worst_profit, lp_chosen,
                        cross_gap, error, computed_time, odds_updated
                 FROM kessbet_match_cost
                 WHERE platform=%s AND match_id=%s"""
        with self.conn.cursor() as cur:
            cur.execute(sql, (platform, match_id))
            r = cur.fetchone()
        if not r:
            return None
        return {
            'cost': float(r[0]) if r[0] is not None else None,
            'cost_scope': r[1], 'n_markets': r[2], 'n_selections': r[3],
            'n_candidates': r[4], 'ilp_chosen': r[5],
            'lp_worst_profit': float(r[6]) if r[6] is not None else None,
            'lp_chosen': r[7],
            'cross_gap': float(r[8]) if r[8] is not None else None,
            'error': r[9], 'computed_time': r[10], 'odds_updated': r[11],
        }

    def cleanup_cost(self, days: int = 7) -> int:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM kessbet_match_cost "
                        "WHERE kickoff_time < NOW() - INTERVAL %s DAY", (days,))
            return cur.rowcount

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

    # ---------- 撤盘日志 ----------

    def log_removed(self, match_id: str, removed: list[tuple],
                    removed_time, source: str = 'http',
                    platform: str = 'kessbet') -> int:
        """把即将被删除的选项记进撤盘日志。

        removed: [(market, side, line, selection, odds), ...]
                 调用方必须在 DELETE 之前把过期的赔率一起传进来 ——
                 删完就查不到了。

        失败只打日志不抛异常：这是观察用的记录，不能因为写不进去
        就让整场刷新失败（那会连带丢掉真正的赔率更新）。
        """
        if not removed:
            return 0
        sql = """
        INSERT IGNORE INTO kessbet_market_removed
          (platform, match_id, market, side, line, selection,
           odds, removed_time, source)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        rows = [(platform, match_id, m, s, l, lab, o, removed_time, source)
                for m, s, l, lab, o in removed]
        try:
            with self.conn.cursor() as cur:
                return cur.executemany(sql, rows)
        except Exception as e:
            log.warning('log_removed %s failed: %s', match_id, e)
            return 0

    # ---------- 套利命中快照 ----------

    def write_arbitrage_snapshot(self, *, match_id: str, sport: str,
                                 home: str, away: str, league: str,
                                 canonical_id: str, kickoff_time,
                                 cost: float, cost_scope: str,
                                 lp_worst_profit, cross_gap,
                                 ilp_chosen: str, lp_chosen: str,
                                 n_markets: int, n_selections: int,
                                 n_candidates: int, market_detail: str,
                                 odds_updated, alert_time,
                                 is_live: int = 0) -> bool:
        """写一条命中快照。返回 True 表示新写入，False 表示这份赔率已记过。

        唯一键 (platform, match_id, odds_updated) 负责去重：
        分析每 2 秒轮询，同一份赔率持续命中时不能反复写 60KB 的快照。

        is_live=1 表示滚球窗口盘命中 —— 证据形态不同（只存命中的那个盘口），
        页面要能分开看。
        """
        sql = """
        INSERT INTO kessbet_arbitrage_snapshot
          (alert_time, platform, match_id, canonical_id, sport,
           home_team, away_team, league, kickoff_time,
           cost, cost_scope, lp_worst_profit, cross_gap,
           ilp_chosen, lp_chosen, n_markets, n_selections, n_candidates,
           market_detail, odds_updated, is_live)
        VALUES (%s,'kessbet',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE id = id
        """
        with self.conn.cursor() as cur:
            n = cur.execute(sql, (
                alert_time, match_id, canonical_id, sport,
                home, away, league, kickoff_time,
                cost, cost_scope, lp_worst_profit, cross_gap,
                ilp_chosen, lp_chosen, n_markets, n_selections, n_candidates,
                market_detail, odds_updated, is_live,
            ))
        self.conn.commit()
        # MySQL: INSERT 返回 1，命中重复键返回 0
        return n == 1

    def list_arbitrage(self, limit: int = 100, offset: int = 0,
                       match_id: str | None = None,
                       is_live: int | None = None) -> list[tuple]:
        """命中列表，按告警时间倒序。不返回 market_detail（太大）。

        is_live: None=全部，0=只看赛前，1=只看滚球
        """
        where = ["platform='kessbet'"]
        args: list = []
        if match_id:
            where.append('match_id=%s')
            args.append(match_id)
        if is_live is not None:
            where.append('is_live=%s')
            args.append(1 if is_live else 0)
        sql = f"""
        SELECT id, alert_time, match_id, canonical_id, sport,
               home_team, away_team, league, kickoff_time,
               cost, cost_scope, lp_worst_profit, cross_gap,
               ilp_chosen, lp_chosen, n_markets, n_selections, n_candidates,
               odds_updated, is_live
        FROM kessbet_arbitrage_snapshot
        WHERE {' AND '.join(where)}
        ORDER BY alert_time DESC, id DESC
        LIMIT %s OFFSET %s
        """
        with self.conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, args + [limit, offset])
            return list(cur.fetchall())

    def count_arbitrage(self, match_id: str | None = None,
                        is_live: int | None = None) -> int:
        where = ["platform='kessbet'"]
        args: list = []
        if match_id:
            where.append('match_id=%s')
            args.append(match_id)
        if is_live is not None:
            where.append('is_live=%s')
            args.append(1 if is_live else 0)
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM kessbet_arbitrage_snapshot "
                        f"WHERE {' AND '.join(where)}", args)
            return cur.fetchone()[0]

    def get_arbitrage(self, snap_id: int) -> tuple | None:
        """单条命中快照的完整数据（含 market_detail）。"""
        sql = """
        SELECT id, alert_time, match_id, canonical_id, sport,
               home_team, away_team, league, kickoff_time,
               cost, cost_scope, lp_worst_profit, cross_gap,
               ilp_chosen, lp_chosen, n_markets, n_selections, n_candidates,
               market_detail, odds_updated, is_live
        FROM kessbet_arbitrage_snapshot
        WHERE platform='kessbet' AND id=%s
        """
        with self.conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, (snap_id,))
            return cur.fetchone()

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

            # 成本流水没有 kickoff_time（它记的是「什么时候变的」），
            # 只能按 changed_time 清。放在 match 之后执行，避免出现
            # 「比赛还在但流水没了」的中间态被别的查询看到。
            cur.execute(
                "DELETE FROM kessbet_match_cost_history "
                "WHERE changed_time < NOW() - INTERVAL %s DAY",
                (history_days,))
            result['cost_history'] = cur.rowcount

            cur.execute(
                "DELETE FROM kessbet_collect_log "
                "WHERE started_time < NOW() - INTERVAL 30 DAY")
            result['log'] = cur.rowcount
        self.conn.commit()
        return result
