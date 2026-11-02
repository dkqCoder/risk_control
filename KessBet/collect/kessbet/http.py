"""collect/kessbet/http.py — KessBet HTTP 接口封装。

请求体格式：request=<urlencode(base64(AES-192-CBC(JSON)))>
响应明文 JSON。
"""
import base64
import json
import logging
import time
import urllib.parse
from datetime import datetime, timezone

from Crypto.Cipher import AES
from curl_cffi import requests as creq

from common.models import Match
from common.timeutil import from_ts
from collect import config
from collect.kessbet.parse import parse_mari, parse_market_id

log = logging.getLogger(__name__)

API = 'https://api.kessbet.com/cpop/ots/unct/public/handler'
KEY = b'QipVAlnxoMjeQ4I47ne7kWDk'      # AES-192，前端硬编码，不过期
IV = b'0000000000000000'

ACTION_LIST = '00252'
ACTION_MATCH = '0015'

HEADERS = {
    'content-type': 'application/x-www-form-urlencoded;charset=UTF-8',
    'origin': 'https://www.kessbet.com',
    'referer': 'https://www.kessbet.com/',
}


class ForbiddenError(Exception):
    """Cloudflare 403——说明代理失效，不是单场问题。"""


def encrypt(payload: dict) -> str:
    raw = json.dumps(payload, separators=(',', ':')).encode()
    pad = 16 - len(raw) % 16
    raw += bytes([pad]) * pad
    ct = AES.new(KEY, AES.MODE_CBC, IV).encrypt(raw)
    b64 = base64.b64encode(ct).decode()
    return 'request=' + urllib.parse.quote(b64, safe='=').replace('+', '%2B')


def _header(action: str) -> dict:
    return {
        'url': '//api.kessbet.com/cpop/ots/unct/public/handler',
        'method': 'post',
        'action': action,
        'isApp': 0,
        'brand': 'KYA',
        'cmdName': 'h5_zz',
        'platformVersion': '1.4.5',
        'appVersion': '1.4.5',
        'platformCode': 'pc',
        'token': '',
        'uuid': 'x',
        'zoneId': 'Asia/Shanghai',
    }


class KessbetHttp:
    def __init__(self, proxy: str | None = None, timeout: float | None = None):
        proxy = proxy or config.PROXY
        proxies = {'https': proxy, 'http': proxy} if proxy else None
        self.session = creq.Session(impersonate='chrome', proxies=proxies)
        self.timeout = timeout or config.HTTP_TIMEOUT

    # ---------- 底层 ----------

    def _post(self, action: str, body: dict) -> dict:
        payload = encrypt({'header': _header(action), 'body': body})
        last_err = None
        for attempt in range(config.HTTP_RETRIES + 1):
            try:
                r = self.session.post(API, data=payload, headers=HEADERS,
                                      timeout=self.timeout)
                if r.status_code == 403:
                    raise ForbiddenError('Cloudflare 403 — 检查代理')
                r.raise_for_status()
                j = r.json()
                if j.get('code') != 1:
                    raise RuntimeError(f'code={j.get("code")} msg={j.get("msg")}')
                return j['data']
            except ForbiddenError:
                raise                       # 不重试
            except Exception as e:
                last_err = e
                if attempt < config.HTTP_RETRIES:
                    time.sleep(1.0 * (attempt + 1))
        raise last_err

    # ---------- 赛程 ----------

    def list_raw(self, tab_id: int) -> list[dict]:
        """拉某个 tabId 的赛程，内部处理游标分页。"""
        base = {'tabId': tab_id, 'playType': '', 'timeslot': 'All', 'leagues': []}
        seen: dict[str, dict] = {}
        last_lid = last_mid = ''

        for _ in range(config.MAX_PAGES):
            body = dict(base)
            if last_lid:
                body['lastLeagueId'] = last_lid
                body['lastMatchId'] = last_mid
            groups = (self._post(ACTION_LIST, body) or {}).get('leagueGroupList') or []
            if not groups:
                break
            new = 0
            for g in groups:
                for m in g.get('matchList') or []:
                    if m['matchId'] not in seen:
                        seen[m['matchId']] = m
                        new += 1
            if new == 0:
                break
            last_lid = str(groups[-1]['li'])
            last_mid = groups[-1]['matchList'][-1]['matchId']

        return list(seen.values())

    def list_matches(self, sport: str) -> list[Match]:
        """拉某运动的赛程，转成统一模型。"""
        tab_id = config.SPORTS[sport]
        out = []
        for raw in self.list_raw(tab_id):
            m = self._to_match(raw, sport)
            if m:
                out.append(m)
        return out

    @staticmethod
    def _to_match(raw: dict, sport: str) -> Match | None:
        try:
            ts = raw.get('mbtp')
            if not ts:
                return None
            kickoff = from_ts(ts)
            return Match(
                platform=config.PLATFORM,
                match_id=raw['matchId'],
                sport=sport,
                league=raw.get('ln') or '',
                country=raw.get('cn') or '',
                home_team=raw.get('htn') or '',
                away_team=raw.get('atn') or '',
                kickoff_time=kickoff,
                sportradar_id=raw.get('tmi') or '',
            )
        except (KeyError, TypeError, ValueError) as e:
            log.warning('bad match record: %s (%s)', raw.get('matchId'), e)
            return None

    # ---------- 单场盘口 ----------

    def fetch_raw(self, match_id: str) -> list[dict]:
        """拉单场全部盘口，返回原始列表。"""
        data = self._post(ACTION_MATCH, {'matchId': match_id})
        return data if isinstance(data, list) else []

    def fetch_odds(self, match_id: str, home: str = '', away: str = '') -> list:
        """拉单场全部盘口，解析成 Selection 列表。

        home / away 用于拆分队名盘口（如 'Arsenal Total' -> side='H'）。
        """
        from common.models import Selection
        from collect.kessbet.parse import split_team

        out = []
        for mk in self.fetch_raw(match_id):
            mari = mk.get('mari') or ''
            name, sels = parse_mari(mari)
            if not name:
                continue
            side, base = split_team(name, home, away)
            for line, label, odds in sels:
                out.append(Selection(base, side, line, label, odds))
        return out
