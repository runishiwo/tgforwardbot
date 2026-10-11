"""转发核心：Telegram 客户端、登录、消息投递、事件监听与断线补扫、日志清理。

`ForwardBot` 只负责「把一条消息发到目的频道」这一件事以及连接的维护；
「哪些消息要转发」由 `monitor.py` / `manual.py` 里的任务决定。
"""

import asyncio
import collections
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

from telethon import TelegramClient, events, utils
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

from core import BASE_DIR, TAG_FAIL, TAG_SKIP, err_text, print_ts, read_lines
from monitor import MonitorTaskManager
from stats import StatsStore
from tasks import (MANUAL, MONITOR, RETENTION_DEFAULT, STATE_LABEL, AnchorStore,
                   TaskLog, TaskStore)
from words import WordScopeStore


class ForwardBot:
    def __init__(self, config):
        self.api_id = config['api_id']
        self.api_hash = config['api_hash']
        # 仅用于首次升级时给「默认实时任务」兜底目的频道
        self.target_entity_id = config.get('target_entity')
        self.keyword_file = config.get('keyword_file', 'keyword.txt')
        self.blocked_file = config.get('blocked_file', 'blocked.txt')
        self.target_file = config.get('target_file', 'targets.txt')
        self.reload_interval = config.get('reload_interval', 60)
        self.forward_interval = config.get('forward_interval', 5)
        self.max_retries = config.get('max_retries', 3)
        # 受保护媒体的「下载 / 上传」单次超时（秒）。家里网络慢、视频较大时 180 秒常常不够，
        # 一超时整条消息就永远发不出去（还被无限重试钉在队头）。默认放宽到 15 分钟。
        self.media_timeout = max(60, int(config.get('media_timeout', 900)))
        self.album_scan_limit = config.get('album_scan_limit', 50)
        self.log_cleanup_interval = config.get('log_cleanup_interval', 6 * 3600)
        self.session_name = config.get('session_name', 'session_session')
        # 自己创建的频道 id 集合（懒加载缓存），「监听全部频道」时用它排除自有频道
        self._own_channel_ids = None

        self.log_dir = os.path.abspath(config.get('log_dir', 'log'))
        os.makedirs(self.log_dir, exist_ok=True)

        # 受保护媒体降级重传的临时落盘目录（发完即删，启动时清残留）
        self.tmp_media_dir = os.path.abspath(config.get('media_tmp_dir', 'media_tmp'))
        os.makedirs(self.tmp_media_dir, exist_ok=True)
        for f in os.listdir(self.tmp_media_dir):
            try:
                os.remove(os.path.join(self.tmp_media_dir, f))
            except OSError:
                pass

        log_formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        log_handler = TimedRotatingFileHandler(
            filename=os.path.join(self.log_dir, 'forward.log'),
            when='midnight', interval=1, backupCount=7, encoding='utf-8')
        log_handler.setFormatter(log_formatter)

        connection_handler = TimedRotatingFileHandler(
            filename=os.path.join(self.log_dir, 'connection.log'),
            when='midnight', interval=1, backupCount=7, encoding='utf-8')
        connection_handler.setFormatter(log_formatter)

        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        logger.addHandler(log_handler)
        logging.getLogger('telethon').setLevel(logging.ERROR)
        self.connection_logger = logging.getLogger('tg_connection')
        self.connection_logger.setLevel(logging.INFO)
        self.connection_logger.propagate = False
        self.connection_logger.addHandler(connection_handler)

        self.log_file = os.path.join(self.log_dir, 'forward.log')

        print_ts('当前工作目录: %s' % os.getcwd())
        print_ts('日志文件路径: %s' % self.log_file)
        print_ts('程序启动，日志初始化成功')

        self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)

        # 词表：实时监控 / 手动转发 各一份关键词表 + 一份屏蔽词表（用旧总表做种子）
        self.word_store = WordScopeStore(BASE_DIR, {
            'keyword': read_lines(self.keyword_file),
            'blocked': read_lines(self.blocked_file),
        })
        self.word_reload_tasks = []

        # 任务中心底座：任务表 / 独立日志 / 转发锚点
        self.task_log = TaskLog(os.path.join(self.log_dir, 'tasks'))
        self.task_store = TaskStore(os.path.join(BASE_DIR, 'data', 'tasks.db'))
        self.anchors = AnchorStore(os.path.join(BASE_DIR, 'data', 'anchors.db'))

        # 实时监控任务运行时（手动转发在 manual.py，由 WebServer 持有）
        self.monitors = MonitorTaskManager(self, self.task_store)

        self.stop_event = asyncio.Event()
        self.auth_lock = asyncio.Lock()
        self.auth_phone = None
        self.auth_phone_code_hash = None
        self.connection_monitor_task = None
        self.recovery_init_task = None
        self.monitor_load_task = None
        self.log_cleanup_task = None
        self.channel_last_seen = {}
        self.recovery_locks = collections.defaultdict(asyncio.Lock)
        self.cursor_db_path = os.path.join(BASE_DIR, 'data', 'recovery_cursors.db')
        os.makedirs(os.path.dirname(self.cursor_db_path), exist_ok=True)
        with sqlite3.connect(self.cursor_db_path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS channel_cursors ('
                         'chat_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, '
                         'updated_at TEXT NOT NULL)')
            self.channel_last_seen = {
                int(chat_id): int(message_id)
                for chat_id, message_id in conn.execute(
                    'SELECT chat_id, message_id FROM channel_cursors')}
        self.stats = StatsStore(os.path.join(BASE_DIR, 'data', 'stats.db'))
        self.started_at = datetime.now()
        self.last_forward_at = None
        self.telegram_authorized = False
        self.connection_state = 'unknown'
        self.disconnect_count = 0
        self.reconnect_count = 0
        self.last_disconnect_at = None
        self.last_reconnect_at = None

        self.client.add_event_handler(self._on_new_message, events.NewMessage())

    # ---------------- 基础工具 ----------------
    def _session_path(self):
        return os.path.abspath(self.session_name + '.session')

    def peer_id(self, peer):
        try:
            return utils.get_peer_id(peer)
        except Exception:
            return int(getattr(peer, 'id', 0) or 0)

    def _dest_id(self, dest):
        return self.peer_id(dest)

    async def resolve_peers(self, ids):
        """把 -100 形式的频道 id 解析成实体，解析失败的记日志跳过。"""
        peers = []
        for pid in ids or []:
            try:
                peers.append(await self.client.get_entity(int(pid)))
            except Exception as exc:
                logging.error('目的频道解析失败: %s (%s)', pid, exc)
        return peers

    def queue_size(self):
        return sum(t.queue.qsize() for t in self.monitors.tasks.values())

    async def own_channel_ids(self, refresh=False):
        """自己创建的频道 id 集合（带缓存）。

        「来源频道留空 = 监听所有频道」时要排除这些频道：手动转发把消息投进自有频道后，
        不该被实时任务再捞一遍（否则会重复转发）。
        """
        if self._own_channel_ids is not None and not refresh:
            return self._own_channel_ids
        ids = set()
        try:
            async for dialog in self.client.iter_dialogs():
                ent = getattr(dialog, 'entity', None)
                if ent is None or not getattr(ent, 'broadcast', False):
                    continue
                if getattr(ent, 'creator', False):
                    ids.add(self.peer_id(ent))
        except Exception as exc:
            logging.warning('统计「自己创建的频道」失败：%s', exc)
            if self._own_channel_ids is not None:
                return self._own_channel_ids
            return set()
        if self._own_channel_ids is None:
            logging.info('自己创建的频道共 %d 个（「监听全部频道」时会自动排除）', len(ids))
        self._own_channel_ids = ids
        return ids

    async def is_own_channel(self, chat_id):
        return chat_id in await self.own_channel_ids()

    def task_counts(self):
        store = self.task_store
        return {
            'total': store.count(),
            'monitor': store.count(MONITOR),
            'manual': store.count(MANUAL),
            'monitor_running': store.count(MONITOR, 'running'),
            'monitor_paused': store.count(MONITOR, 'paused'),
            'manual_running': store.count(MANUAL, 'running'),
            'manual_paused': store.count(MANUAL, 'paused'),
        }

    def kind_counts(self, kind):
        items = self.task_store.all(kind)
        out = {'total': len(items)}
        for st in ('running', 'paused', 'done', 'cancelled', 'failed', 'interrupted'):
            out[st] = sum(1 for x in items if x['state'] == st)
        return out

    def kind_stats(self, kind):
        agg = {'checked': 0, 'matched': 0, 'sent': 0, 'failed': 0, 'skipped': 0}
        for t in self.task_store.all(kind):
            s = t.get('stats') or {}
            for k in agg:
                agg[k] += int(s.get(k, 0) or 0)
        return agg

    # ---------------- 登录 ----------------
    async def prepare_login(self):
        """断开当前客户端，用一个干净的客户端走网页登录。"""
        await self.pause_runtime()
        session_path = self._session_path()
        if os.path.exists(session_path):
            backup = '%s.bak.%d' % (session_path, int(time.time()))
            os.replace(session_path, backup)
            logging.info('旧 Telegram session 已备份: %s', backup)
        self.client = TelegramClient(self.session_name, self.api_id, self.api_hash)
        self.client.add_event_handler(self._on_new_message, events.NewMessage())

    async def finish_web_login(self):
        await self.client.connect()
        self.telegram_authorized = True
        self.stop_event.clear()
        self._start_tasks()
        self.connection_state = 'connected' if self.client.is_connected() else 'disconnected'
        self.connection_monitor_task = asyncio.create_task(self._connection_monitor())
        self.recovery_init_task = asyncio.create_task(self._initialize_recovery())

    async def telegram_auth_status(self):
        authorized = await self.client.is_user_authorized()
        self.telegram_authorized = authorized
        return {
            'authorized': authorized,
            'waiting_code': bool(self.auth_phone_code_hash),
            'phone': self.auth_phone or '',
        }

    # ---------------- 迁移：默认实时任务 ----------------
    async def _ensure_default_task(self):
        """升级后若一条实时任务都没有，用原有配置建一条「默认实时任务」，保持原行为。"""
        if self.task_store.all(MONITOR):
            return
        targets = []
        for line in read_lines(self.target_file):
            if line.startswith('#'):
                continue
            try:
                pid = int(line.lstrip('@'))
            except ValueError:
                continue
            targets.append({'id': pid, 'title': line})
        if not targets and self.target_entity_id:
            try:
                targets.append({'id': int(self.target_entity_id), 'title': 'config.yml 默认目标'})
            except (TypeError, ValueError):
                pass
        if not targets:
            print_ts('未找到可用的默认目的频道，请到「实时任务」页面手动创建任务。')
            return
        try:  # 尽量把标题补全，列表里更好认
            peers = await self.resolve_peers([t['id'] for t in targets])
            by_id = {self.peer_id(p): p for p in peers}
            for t in targets:
                p = by_id.get(t['id'])
                if p is not None:
                    t['title'] = getattr(p, 'title', '') or t['title']
        except Exception:
            pass
        record = await self.monitors.create('默认实时任务', [], targets, [], [])
        self.task_log.append(record['id'], '升级自动创建：来源=全部频道，目的 %d 个，'
                                           '关键词/屏蔽词沿用「实时监控」大类词表' % len(targets))
        print_ts('已自动创建「默认实时任务」（来源：全部频道，目的 %d 个）' % len(targets))

    # ---------------- 转发核心 ----------------
    @staticmethod
    def _is_protected_error(e):
        s = str(e).lower()
        t = s.replace('_', '').replace(' ', '')
        return ('forwardsrestricted' in t or 'protected' in t
                or 'noforwards' in t or 'filereference' in t)

    async def _resend_single(self, m, dest):
        # 先尝试引用重发（普通频道秒转、零磁盘）；
        # 受保护频道的媒体引用会被服务器拒绝，自动降级：下载到本地再作为新文件上传。
        # 返回值说明本条消息实际走的路径：
        #   'direct'   —— 直接转发（文本直发 / 引用重发 / forward_messages），不落本机磁盘；
        #   'download' —— 降级为「下载到本机再上传」，慢但能绕过引用限制。
        # 上层据此把「成功」日志分成「直转」和「下载转」两类。
        text = m.message or ''
        if m.media is None:
            await self.client.send_message(dest, text)
            return 'direct'
        if isinstance(m.media, (MessageMediaPhoto, MessageMediaDocument)):
            try:
                await asyncio.wait_for(self.client.send_file(dest, m.media, caption=text or ''),
                                       timeout=self.media_timeout)
                return 'direct'
            except Exception as e:
                if not self._is_protected_error(e):
                    raise
                logging.info('消息ID %s 媒体引用重发受限，降级为下载后重传...', m.id)
            logging.info('消息ID %s 开始下载媒体', m.id)
            path = await asyncio.wait_for(self.client.download_media(m, file=self.tmp_media_dir),
                                          timeout=self.media_timeout)
            if not path:
                raise RuntimeError('消息ID %s 媒体下载返回空路径' % m.id)
            logging.info('消息ID %s 媒体下载完成，开始上传', m.id)
            try:
                await asyncio.wait_for(self.client.send_file(dest, path, caption=text or ''),
                                       timeout=self.media_timeout)
                logging.info('消息ID %s 媒体上传完成', m.id)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
            return 'download'
        # 投票/位置/联系人等没有可下载的文件，只能 forward，受限时由上层捕获
        await self.client.forward_messages(dest, m)
        return 'direct'

    async def deliver(self, chat, message, dests, log=None, retry_forever=False, gate=None,
                      lock=None):
        """把一条消息投递到任务自己的目的频道列表。

        返回值：
          False       —— 投递失败（超过重试次数，或受保护且降级也失败）；
          'direct'    —— 成功，且**全程直接转发**（未落本机磁盘）；
          'download'  —— 成功，但其中至少有一条消息走了「下载后重新上传」的降级路径。
        上层用这个返回值把日志分成「直转」/「下载转」两类（`if ok:` 依然成立）。

        retry_forever=True 时，单条消息失败会**一直重试**（退避最长 60 秒）而不是放弃——
        手动转发用这个模式：宁可慢慢发，也不要把消息丢掉。
        gate 是可选协程（如「等待任务恢复」），每次重试前先 await，保证暂停能被尊重。
        lock 是可选 asyncio.Lock：**只在真正发送的那一下持锁**，重试退避期间让出，
        这样一条卡住的消息不会把其它任务的发送/暂停也堵死。
        """
        dests = [d for d in (dests or []) if d is not None]
        if not dests:
            if log:
                log('该任务没有可用的目的频道，跳过消息 %s' % getattr(message, 'id', '?'),
                    tag=TAG_SKIP)
            return False
        retries = 0
        sent_ids = set()
        last_error = None

        async def _attempt():
            """投递一轮；返回 'direct' / 'download'（只要有一条走下载重传就算 download）。"""
            pending = [d for d in dests if self._dest_id(d) not in sent_ids]
            if not pending:
                return 'direct'
            kinds = set()
            for dest in pending:
                if message.grouped_id:
                    # 相册内各条消息的 id 是连续的：以本条为中心取一个小窗口即可整组找回，
                    # 与它在频道里的位置无关（因此历史里较早的相册也能整组转发）。
                    span = 12
                    msg_ids = []
                    async for msg in self.client.iter_messages(
                            chat, min_id=message.id - span - 1,
                            max_id=message.id + span + 1, limit=span * 2 + 2):
                        if msg is not None and msg.grouped_id == message.grouped_id:
                            msg_ids.append(msg.id)
                    if message.id not in msg_ids:
                        msg_ids.append(message.id)
                    msg_ids.sort()
                    album = await self.client.get_messages(chat, ids=msg_ids)
                    for m in album:
                        if m is not None:
                            kinds.add(await self._resend_single(m, dest))
                else:
                    kinds.add(await self._resend_single(message, dest))
                sent_ids.add(self._dest_id(dest))
            return 'download' if 'download' in kinds else 'direct'

        while True:
            if not retry_forever and retries >= self.max_retries:
                break
            try:
                if lock is not None:
                    async with lock:
                        kind = await _attempt()
                else:
                    kind = await _attempt()
                return kind
            except Exception as e:
                if self._is_protected_error(e):
                    if log:
                        log('消息 %s 受保护且降级重传失败，跳过：%s' % (message.id, e),
                            tag=TAG_FAIL)
                    return False
                last_error = e
                retries += 1
                if retry_forever:
                    delay = min(3 * retries, 60)
                    # 无限重试时日志只记前 10 次、之后每 10 次记一条，避免刷屏
                    if log and (retries <= 10 or retries % 10 == 0):
                        log('消息 %s 转发失败（第 %d 次重试，%d 秒后再试）：%s'
                            % (message.id, retries, delay, err_text(e)), tag=TAG_FAIL)
                    logging.warning('转发消息ID %s 失败，第 %d 次重试（持续重试不放弃），%d 秒后再试，原因：%s',
                                    message.id, retries, delay, e)
                else:
                    delay = 3
                    if log:
                        log('消息 %s 转发失败（第 %d 次重试）：%s'
                            % (message.id, retries, err_text(e)), tag=TAG_FAIL)
                    logging.error('转发消息ID %s 失败，第 %d 次重试，原因：%s', message.id, retries, e)
                if gate is not None:
                    await gate()
                await asyncio.sleep(delay)
        if log:
            log('消息 %s 超过最大重试次数(%d)，放弃：%s' % (message.id, self.max_retries, last_error),
                tag=TAG_FAIL)
        logging.error('消息ID %s 转发失败，超过最大重试次数(%d)。', message.id, self.max_retries)
        return False

    # ---------------- 事件与补扫 ----------------
    async def _on_new_message(self, event):
        try:
            chat = await event.get_chat()
        except Exception:
            return
        if not getattr(chat, 'broadcast', False):
            return
        message = event.message
        async with self.recovery_locks[chat.id]:
            await self.monitors.dispatch(chat, message)
            self._save_channel_cursor(chat.id, message.id)

    def _save_channel_cursor(self, chat_id, message_id):
        current = self.channel_last_seen.get(chat_id, 0)
        if message_id <= current:
            return
        self.channel_last_seen[chat_id] = message_id
        with sqlite3.connect(self.cursor_db_path, timeout=10) as conn:
            conn.execute(
                'INSERT INTO channel_cursors(chat_id,message_id,updated_at) VALUES(?,?,?) '
                'ON CONFLICT(chat_id) DO UPDATE SET message_id=MAX(message_id,excluded.message_id), '
                'updated_at=excluded.updated_at',
                (chat_id, message_id, datetime.now().isoformat()))

    async def _load_channel_baseline(self):
        initialized = 0
        allowed = self.monitors.source_ids()
        async for dialog in self.client.iter_dialogs():
            chat = dialog.entity
            if not getattr(chat, 'broadcast', False):
                continue
            if allowed is not None and chat.id not in allowed:
                continue
            if self.channel_last_seen.get(chat.id, 0) > 0:
                continue
            try:
                latest = await self.client.get_messages(chat, limit=1)
                if latest:
                    self._save_channel_cursor(chat.id, latest[0].id)
                    initialized += 1
            except Exception as exc:
                self.connection_logger.warning(
                    '频道 %s 基线记录失败: %s', getattr(chat, 'title', chat.id), exc)
        if initialized:
            self.connection_logger.info('已为 %d 个频道建立补扫基线', initialized)

    async def _initialize_recovery(self):
        try:
            await self._load_channel_baseline()
            await self._backfill_after_reconnect(reason='启动恢复')
        except Exception as exc:
            self.connection_logger.error('连接监控初始化失败: %s', exc)

    async def _backfill_after_reconnect(self, reason='重连'):
        checked = 0
        try:
            allowed = self.monitors.source_ids()
            async for dialog in self.client.iter_dialogs():
                chat = dialog.entity
                if not getattr(chat, 'broadcast', False):
                    continue
                if allowed is not None and chat.id not in allowed:
                    continue
                start_id = self.channel_last_seen.get(chat.id, 0)
                channel_checked = 0
                async with self.recovery_locks[chat.id]:
                    while self.client.is_connected():
                        batch = []
                        async for message in self.client.iter_messages(
                                chat, min_id=start_id, reverse=True, limit=200):
                            batch.append(message)
                        if not batch:
                            break
                        for message in batch:
                            await self.monitors.dispatch(chat, message)
                            self._save_channel_cursor(chat.id, message.id)
                            start_id = max(start_id, message.id)
                            channel_checked += 1
                            checked += 1
                        self.connection_logger.info(
                            '%s进度: 频道 %s 已检查 %d 条，游标 %s',
                            reason, getattr(chat, 'title', chat.id), channel_checked, start_id)
                        if len(batch) < 200:
                            break
                if not self.client.is_connected():
                    self.connection_logger.warning('%s中断: Telegram 再次断开，已检查 %d 条', reason, checked)
                    return
            self.connection_logger.info('%s完成: 检查 %d 条新消息', reason, checked)
        except Exception as exc:
            self.connection_logger.error('%s失败: 检查 %d 条后异常: %s', reason, checked, exc)

    async def _connection_monitor(self):
        previous = self.client.is_connected()
        self.connection_state = 'connected' if previous else 'disconnected'
        while not self.stop_event.is_set():
            await asyncio.sleep(5)
            current = self.client.is_connected()
            if current == previous:
                continue
            previous = current
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            if current:
                self.connection_state = 'connected'
                self.reconnect_count += 1
                self.last_reconnect_at = now
                self.connection_logger.info(
                    '连接恢复: 第 %d 次恢复，累计断线 %d 次', self.reconnect_count, self.disconnect_count)
                await self._backfill_after_reconnect(reason='重连')
            else:
                self.connection_state = 'disconnected'
                self.disconnect_count += 1
                self.last_disconnect_at = now
                self.connection_logger.warning('连接断开: 第 %d 次', self.disconnect_count)

    # ---------------- 日志保留清理 ----------------
    async def _log_cleanup_loop(self):
        interval = max(300, int(self.log_cleanup_interval))
        while not self.stop_event.is_set():
            try:
                await self.cleanup_logs()
            except Exception as exc:
                logging.error('日志清理失败: %s', exc)
            try:
                await self.own_channel_ids(refresh=True)     # 顺手刷新自建频道集合
            except Exception as exc:
                logging.warning('刷新自建频道集合失败: %s', exc)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def cleanup_logs(self):
        """按每条任务的「日志保留天数」清理超期日志。"""
        dropped = 0
        for task in self.task_store.all():
            keep = task.get('keep_days', RETENTION_DEFAULT)
            if not keep:
                continue
            n = self.task_log.prune(task['id'], keep)
            if n:
                dropped += n
                self.task_log.append(task['id'], '已按「日志保留 %d 天」清理 %d 行历史日志' % (keep, n))
        if dropped:
            print_ts('日志清理完成：共清理 %d 行超期日志' % dropped)
        self._purge_orphan_files()
        return dropped

    def _purge_orphan_files(self):
        """清掉已删除任务遗留的文件：续跑队列 `log/tasks/<id>.queue` 与日志目录 `log/tasks/<id>/`。"""
        ids = {t['id'] for t in self.task_store.all()}
        folder = os.path.join(BASE_DIR, 'log', 'tasks')
        try:
            names = os.listdir(folder)
        except OSError:
            return
        for name in names:
            path = os.path.join(folder, name)
            if name.endswith('.queue'):
                if name[:-len('.queue')] not in ids:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            elif name.endswith('.log'):
                if name[:-len('.log')] not in ids:      # 旧格式孤儿文件
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            elif os.path.isdir(path) and name not in ids:
                shutil.rmtree(path, ignore_errors=True)

    # ---------------- 生命周期 ----------------
    def _log_word_rules(self):
        for scope in ('monitor', 'manual'):
            label = {'monitor': '实时监控', 'manual': '手动转发'}[scope]
            kw_on = self.word_store.kind_enabled(scope, 'keyword')
            bl_on = self.word_store.kind_enabled(scope, 'blocked')
            print_ts('【词表规则】%s → 关键词：%s；屏蔽词：%s' % (
                label,
                '启用' if kw_on else '未启用（不做关键词过滤）',
                '启用（命中即不转发，优先于关键词）' if bl_on else '未启用'))

    def _log_task_summary(self):
        for record in self.task_store.all(MONITOR):
            print_ts('【实时任务】%s [%s] %s' % (
                record['name'], STATE_LABEL.get(record['state'], record['state']),
                '%d 个目的频道' % len((record.get('config') or {}).get('targets') or [])
                if (record.get('config') or {}).get('targets') else '无目的频道'))

    def _start_tasks(self):
        self.word_reload_tasks = [
            asyncio.create_task(m.reload_periodically(self.reload_interval))
            for m in self.word_store.managers_list()
        ]
        self.monitor_load_task = asyncio.create_task(self.monitors.load_all())
        self.log_cleanup_task = asyncio.create_task(self._log_cleanup_loop())

    async def start(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            print_ts('Telegram 尚未登录，请通过 WebUI 的 Telegram 登录页面完成登录。')
            return False
        self.telegram_authorized = True
        self.task_log.migrate()          # 旧版单文件日志 → 按日分文件的目录结构
        await self._ensure_default_task()
        self._log_word_rules()
        self._log_task_summary()
        print_ts('开始监听频道消息，按各任务规则过滤后转发（多任务并行）...')
        self.stop_event.clear()
        self._start_tasks()
        # 预热「自己创建的频道」缓存，避免第一条自有频道消息才去遍历 dialogs
        asyncio.create_task(self.own_channel_ids())
        self.connection_state = 'connected' if self.client.is_connected() else 'disconnected'
        self.connection_monitor_task = asyncio.create_task(self._connection_monitor())
        self.recovery_init_task = asyncio.create_task(self._initialize_recovery())
        return True

    async def pause_runtime(self):
        self.stop_event.set()
        tasks = list(self.word_reload_tasks)
        for t in (self.monitor_load_task, self.connection_monitor_task,
                  self.recovery_init_task, self.log_cleanup_task):
            if t:
                tasks.append(t)
        for task in tasks:
            if task:
                task.cancel()
        await self.monitors.stop_all()
        await asyncio.gather(*(t for t in tasks if t), return_exceptions=True)
        self.word_reload_tasks = []
        self.monitor_load_task = None
        self.connection_monitor_task = None
        self.recovery_init_task = None
        self.log_cleanup_task = None
        try:
            await self.client.disconnect()
        except Exception:
            pass

    async def stop(self):
        await self.pause_runtime()

    def connection_status(self):
        return {
            'state': self.connection_state,
            'disconnect_count': self.disconnect_count,
            'reconnect_count': self.reconnect_count,
            'last_disconnect_at': self.last_disconnect_at,
            'last_reconnect_at': self.last_reconnect_at,
            'channels_tracked': len(self.channel_last_seen),
        }
