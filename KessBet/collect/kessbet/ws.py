"""collect/kessbet/ws.py — KessBet WebSocket 客户端。

只负责两件事：
1. 维持连接（握手、心跳、重连）
2. 把推送里出现的 match_id 交给回调

**不解析推送内容**。推送的 update/insert/delete 一律不看——
数据一律由 HTTP 全量接口提供。这是架构的核心决策：
推送告诉我们"哪场变了"，HTTP 告诉我们"变成了什么"。

socket.io v2 帧格式：
    0{...}          Engine.IO 握手
    2               ping（必须客户端主动发，服务端回 3）
    3               pong
    40              命名空间连接成功
    41              断开（服务端踢人）
    42["event",...] 事件
"""
import json
import logging
import threading
import time
from typing import Callable

from curl_cffi import WebSocket

from collect import config

log = logging.getLogger(__name__)


class KessbetWs:
    def __init__(self, on_change: Callable[[str], None],
                 proxy: str | None = None,
                 url: str | None = None):
        """
        on_change: 收到 bet_change 时回调，参数是 match_id。
        """
        self.on_change = on_change
        self.url = url or config.WS_URL
        proxy = proxy or config.PROXY
        self.proxies = {'https': proxy, 'http': proxy} if proxy else None

        self._ws: WebSocket | None = None
        self._running = False
        self._subscribed: set[str] = set()
        self._lock = threading.Lock()
        self._hb_thread: threading.Thread | None = None
        self._recv_thread: threading.Thread | None = None

        # 统计
        self.stats = {'pings': 0, 'frames': 0, 'pushes': 0, 'reconnects': 0}

    # ---------- 连接 ----------

    def connect(self) -> None:
        self._ws = WebSocket()
        self._ws.connect(self.url, proxies=self.proxies, impersonate='chrome')
        # 握手前两帧可能是 0{...} 和 40，接收循环会处理
        log.info('ws connected')

    def _wait_connected(self, timeout: float = 15.0) -> bool:
        """等待收到 40（命名空间连接成功）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, _ = self._ws.recv()
            except Exception as e:
                log.warning('wait_connected recv error: %s', e)
                return False
            if data is None:
                return False
            raw = data.decode('utf-8', 'replace') if isinstance(data, bytes) else data
            if raw.startswith('40'):
                return True
        return False

    # ---------- 心跳 ----------

    def _heartbeat_loop(self) -> None:
        """服务端不会主动 ping，客户端必须主动发 2。

        不发的话约 85 秒（25s pingInterval + 60s pingTimeout）后被踢。
        """
        while self._running:
            time.sleep(config.WS_HEARTBEAT_INTERVAL)
            if not self._running:
                break
            try:
                self._ws.send_str('2')
                self.stats['pings'] += 1
            except Exception as e:
                log.warning('heartbeat failed: %s', e)
                self._running = False
                break

    # ---------- 订阅 ----------

    def subscribe(self, match_ids: list[str], batch_size: int | None = None) -> int:
        """累加式订阅。只发送尚未订阅的 ID。返回本次新订阅数。"""
        batch_size = batch_size or config.WS_SUBSCRIBE_BATCH
        with self._lock:
            new = [m for m in match_ids if m not in self._subscribed]
            self._subscribed.update(new)

        if not new:
            return 0

        sent = 0
        for i in range(0, len(new), batch_size):
            chunk = new[i:i + batch_size]
            payload = json.dumps({'msgContent': ','.join(chunk)},
                                 separators=(',', ':'))
            try:
                self._ws.send_str(f'42["watch_match",{payload}]')
                sent += len(chunk)
            except Exception as e:
                log.error('subscribe failed: %s', e)
                break
        log.info('subscribed %d matches (%d total)', sent, len(self._subscribed))
        return sent

    def _resubscribe_all(self) -> None:
        """重连后服务端不记得旧订阅，需要全部重发。"""
        with self._lock:
            ids = list(self._subscribed)
            self._subscribed.clear()
        if ids:
            self.subscribe(ids)

    def prune(self, keep: set[str]) -> int:
        """本地剔除已结束的比赛（不通知服务端）。"""
        with self._lock:
            before = len(self._subscribed)
            self._subscribed &= keep
            return before - len(self._subscribed)

    # ---------- 接收 ----------

    @staticmethod
    def extract_match_id(raw: str) -> str | None:
        """从 bet_change 帧里只取 matchId。

        这是推送相关的**全部**解析逻辑——其余内容不看。
        """
        if not raw.startswith('42["bet_change"'):
            return None
        try:
            arr = json.loads(raw[2:])
            body = arr[1]
            if isinstance(body, str):
                body = json.loads(body)
            return body.get('matchId')
        except (ValueError, IndexError, AttributeError, TypeError):
            return None

    def _recv_loop(self) -> None:
        while self._running:
            try:
                data, _ = self._ws.recv()
            except Exception as e:
                if self._running:
                    log.warning('recv error: %s', e)
                break

            if data is None:
                if self._running:
                    log.warning('connection closed by peer')
                break

            raw = data.decode('utf-8', 'replace') if isinstance(data, bytes) else data
            self.stats['frames'] += 1

            if raw == '2':
                try:
                    self._ws.send_str('3')
                except Exception:
                    break
                continue
            if raw.startswith('3'):
                continue
            if raw == '41' or raw.startswith('41'):
                log.warning('server sent disconnect (41)')
                break

            match_id = self.extract_match_id(raw)
            if match_id:
                self.stats['pushes'] += 1
                try:
                    self.on_change(match_id)
                except Exception:
                    log.exception('on_change callback failed for %s', match_id)

        self._running = False

    # ---------- 生命周期 ----------

    def start(self, initial_ids: list[str] | None = None) -> None:
        """建立连接、订阅、启动心跳与接收线程。"""
        self._running = True
        self.connect()
        if not self._wait_connected():
            self._running = False
            raise RuntimeError('ws handshake failed (no 40 frame)')

        if initial_ids:
            self.subscribe(initial_ids)

        self._hb_thread = threading.Thread(target=self._heartbeat_loop,
                                           daemon=True, name='ws-heartbeat')
        self._hb_thread.start()
        self._recv_thread = threading.Thread(target=self._recv_loop,
                                             daemon=True, name='ws-recv')
        self._recv_thread.start()

    def run_forever(self, initial_ids: list[str] | None = None) -> None:
        """阻塞运行，断开后指数退避重连。

        重连后重新订阅全部，并触发 on_reconnect 回调（用于全量刷新）。
        """
        backoff = 1
        while True:
            try:
                self.start(initial_ids)
                initial_ids = None          # 只有首次需要传
                while self._running and self._recv_thread.is_alive():
                    time.sleep(1)
                log.warning('ws disconnected, will reconnect')
            except Exception as e:
                log.error('ws start failed: %s', e)

            self.stop()
            self.stats['reconnects'] += 1
            time.sleep(backoff)
            backoff = min(backoff * 2, config.WS_RECONNECT_BACKOFF_MAX)
            self._resubscribe_all()

    def stop(self) -> None:
        self._running = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    @property
    def subscribed_count(self) -> int:
        with self._lock:
            return len(self._subscribed)
