"""实时监控任务：每条任务独立运行，可同时跑多条，互不影响。

- 每条任务有自己的来源频道、目的频道、自带词表（留空则用「实时监控」大类词表）、
  关键词/屏蔽词开关、**独立队列 + 独立 worker + 独立日志**；
- 支持**暂停 / 恢复**（停用后不再接收新消息，恢复后继续）；
- 支持**编辑**已有任务；
- 转发成功后在「锚点」里按源频道记一笔，重启/恢复/重复扫描都不会重复转发。

本模块同时提供 `/api/tasks/monitor*` 接口。
"""

import asyncio
import collections
from datetime import datetime

from aiohttp import web

from core import (TAG_BLOCK, TAG_FAIL, TAG_OK_DIRECT, TAG_OK_DOWNLOAD, TAG_SKIP, id_range_text,
                  json_response,
                  preview_text, server_of, split_lines, split_words)
from tasks import (MONITOR, KIND_SCOPE, clean_words, clamp_keep_days, list_public,
                   match_any, norm_peers, task_summary)

_SCOPE = KIND_SCOPE[MONITOR]


class MonitorTask:
    """单条实时监控任务。"""

    def __init__(self, bot, store, record):
        self.bot = bot
        self.store = store
        self.record = record
        self.id = record['id']
        self.queue = asyncio.Queue()
        self.worker = None
        self._stopped = True
        self.seen = collections.OrderedDict()
        self._pending_ids = []       # 已入队但还没汇总记日志的消息 id
        self._dests = None
        self.apply_config()

    # ---------- 配置 ----------
    def apply_config(self):
        cfg = self.record.get('config') or {}
        self.sources = {int(x['id']) for x in (cfg.get('sources') or [])}
        self.all_sources = not self.sources
        self.targets = [int(x['id']) for x in (cfg.get('targets') or [])]
        self.target_meta = list(cfg.get('targets') or [])
        self.keywords = clean_words(cfg.get('keywords'))
        self.blocked = clean_words(cfg.get('blocked'))
        # None = 跟随「实时监控」大类开关；True/False = 本条任务显式启用/停用
        self.use_keyword = cfg.get('use_keyword')
        self.use_blocked = cfg.get('use_blocked')
        self.keep_days = clamp_keep_days(self.record.get('keep_days'))
        self.anchor_on = bool(self.record.get('anchor', True))
        self.global_dedup = bool(self.record.get('global_dedup', True))
        self._dests = None

    # ---------- 生命周期 ----------
    @property
    def running(self):
        return self.record['state'] == 'running' and self.worker is not None

    @property
    def busy(self):
        return self.worker is not None and not self.worker.done()

    async def start(self):
        if self.busy:
            return
        self._stopped = False
        self.worker = asyncio.create_task(self._run())

    async def stop(self, join=False):
        self._stopped = True
        worker, self.worker = self.worker, None
        if worker and not worker.done():
            worker.cancel()
            if join:
                await asyncio.gather(worker, return_exceptions=True)

    # ---------- 词表 ----------
    async def effective_keywords(self):
        if self.keywords:
            return self.keywords
        return await self.bot.word_store.words(MONITOR, 'keyword')

    async def effective_blocked(self):
        if self.blocked:
            return self.blocked
        return await self.bot.word_store.words(MONITOR, 'blocked')

    def keyword_on(self):
        if self.use_keyword is not None:
            return bool(self.use_keyword)
        return bool(self.keywords) or self.bot.word_store.kind_enabled(MONITOR, 'keyword')

    def blocked_on(self):
        if self.use_blocked is not None:
            return bool(self.use_blocked)
        return bool(self.blocked) or self.bot.word_store.kind_enabled(MONITOR, 'blocked')

    # ---------- 匹配 ----------
    def accepts(self, chat_id, username='', title=''):
        if self.all_sources:
            return True
        if chat_id in self.sources:
            return True
        for src in (self.record.get('config') or {}).get('sources') or []:
            uname = str(src.get('username') or '').lstrip('@').lower()
            if uname and username and uname == username.lower():
                return True
        return False

    def is_duplicate(self, chat_id, message_id):
        """本次运行内的快速去重（进程重启后由锚点兜底）。"""
        key = (chat_id, message_id)
        if key in self.seen:
            return True
        self.seen[key] = None
        while len(self.seen) > 8000:
            self.seen.popitem(last=False)
        return False

    async def resolve_dests(self, refresh=False):
        if self._dests is None or refresh:
            self._dests = await self.bot.resolve_peers(self.targets)
        return self._dests

    def log(self, text, tag=None):
        line = self.bot.task_log.append(self.id, text, tag=tag)
        self.store.add_recent(self.record, line)
        return line

    def _flush_enqueue_log(self):
        """把这一批入队的消息汇总成一行日志（不逐条刷屏）。"""
        ids, self._pending_ids = self._pending_ids, []
        if not ids:
            return
        self.log('%s 进入队列（待转发 %d 条）' % (id_range_text(ids), len(ids)))

    # ---------- 消息处理 ----------
    async def offer(self, chat, message):
        """事件入口：按本任务的规则筛选，命中则入队。"""
        if not self.running:
            return
        chat_id = self.bot.peer_id(chat)
        # 来源留空 = 监听所有频道，但排除「自己创建的频道」：
        # 手动转发刚投进自有频道的消息，不该被实时任务再捞一遍（会重复转发）。
        # 若来源是手动勾选的，则用户明确要监听的频道一律不排除。
        if self.all_sources and await self.bot.is_own_channel(chat_id):
            return
        if not self.accepts(chat_id, getattr(chat, 'username', '') or '',
                            getattr(chat, 'title', '') or ''):
            return
        text = message.message or ''
        # 锚点：这条源消息是否已经转发过（本任务 / 跨任务）
        if self.anchor_on and self.bot.anchors.seen(
                self.id, chat_id, message.id, self.global_dedup):
            self.store.stats_add(self.record, 'skipped')
            self.bot.stats.inc('skipped', scope=_SCOPE)
            self.log('消息 %s 已转发过（源频道锚点），跳过' % message.id, TAG_SKIP)
            return
        self.store.stats_add(self.record, 'checked')
        if self.keyword_on():
            words = await self.effective_keywords()
            if not match_any(text, words):
                return
            self.store.stats_add(self.record, 'matched')
        if self.blocked_on():
            bids = await self.effective_blocked()
            if match_any(text, bids):
                self.store.stats_add(self.record, 'skipped')
                self.bot.stats.inc('skipped', scope=_SCOPE)
                reason = '命中关键词但同时命中屏蔽词' if self.keyword_on() else '命中屏蔽词'
                self.log('消息 %s %s，不转发：%s' % (message.id, reason, preview_text(text)),
                         TAG_BLOCK)
                return
        if self.is_duplicate(chat_id, message.id):
            self.store.stats_add(self.record, 'skipped')
            self.bot.stats.inc('skipped', scope=_SCOPE)
            self.log('消息 %s 已在任务内处理过，跳过' % message.id, TAG_SKIP)
            return
        await self.queue.put((chat, message))
        self._pending_ids.append(message.id)   # 汇总成一行「进入队列」日志

    async def _run(self):
        while not self._stopped:
            try:
                chat, message = await asyncio.wait_for(self.queue.get(), timeout=1)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            self._flush_enqueue_log()    # 先汇总「进入队列」，再开始转发，保证日志顺序
            try:
                dests = await self.resolve_dests()
                # 与手动转发一致：网络抖动 / 被限流时**一直重试**（退避最长 60 秒），
                # 不因几次失败就把消息丢掉。停用任务时会直接取消 worker，不会卡住。
                ok = await self.bot.deliver(chat, message, dests, log=self.log,
                                            retry_forever=True)
                if ok:
                    self.store.stats_add(self.record, 'sent')
                    self.bot.stats.inc('forwarded', scope=_SCOPE)
                    self.bot.last_forward_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    if self.anchor_on:
                        self.bot.anchors.advance(self.id, self.bot.peer_id(chat), message.id)
                    titles = '、'.join(x.get('title', '') for x in self.target_meta) or '默认目的'
                    # 按实际转发路径分两类日志：直接转发 / 下载后转发（见 bot.deliver 返回值）
                    if ok == 'download':
                        how, tag = '下载后转发成功', TAG_OK_DOWNLOAD
                    else:
                        how, tag = '转发成功', TAG_OK_DIRECT
                    self.log('消息 %s %s → %s（%s）：%s' % (
                        message.id, how, titles, getattr(chat, 'title', chat.id),
                        preview_text(message.message)), tag)
                else:
                    self.store.stats_add(self.record, 'failed')
                    self.bot.stats.inc('failed', scope=_SCOPE)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.stats_add(self.record, 'failed')
                self.bot.stats.inc('failed', scope=_SCOPE)
                self.log('消息 %s 转发异常：%s' % (getattr(message, 'id', '?'), exc), TAG_FAIL)
            finally:
                self.store.save(self.record)
            await asyncio.sleep(self.bot.forward_interval)


class MonitorTaskManager:
    """多条实时监控任务的运行时管理。"""

    def __init__(self, bot, store):
        self.bot = bot
        self.store = store
        self.tasks = {}

    async def load_all(self):
        for record in self.store.all(MONITOR):
            if record['id'] in self.tasks:   # 已由 create() 启动过，避免重复 worker
                continue
            task = self._make(record)
            self.tasks[task.id] = task
            if record['state'] == 'running':
                await task.start()

    def _make(self, record):
        return MonitorTask(self.bot, self.store, record)

    def get(self, task_id):
        return self.tasks.get(task_id)

    def ensure(self, task_id):
        """取运行时对象；不在内存里就按库里的记录补一个。"""
        task = self.tasks.get(task_id)
        if task is None:
            record = self.store.get(task_id)
            if record is None or record['kind'] != MONITOR:
                return None
            task = self._make(record)
            self.tasks[task_id] = task
        return task

    def running_tasks(self):
        return [t for t in self.tasks.values() if t.running]

    def source_ids(self):
        """所有任务来源频道的并集；有任一任务监控全部频道时返回 None。"""
        ids = set()
        for task in self.tasks.values():
            if not task.running:
                continue
            if task.all_sources:
                return None
            ids |= task.sources
        return ids

    # ---------- 增删改 ----------
    async def create(self, name, sources, targets, keywords, blocked, state='running',
                     use_keyword=True, use_blocked=True, keep_days=None,
                     anchor=True, global_dedup=True):
        record = self.store.create(MONITOR, name, {
            'sources': list(sources or []),
            'targets': list(targets or []),
            'keywords': clean_words(split_words(keywords)),
            'blocked': clean_words(split_words(blocked)),
            'use_keyword': bool(use_keyword),
            'use_blocked': bool(use_blocked),
        }, state=state,
            keep_days=clamp_keep_days(keep_days),
            anchor=anchor, global_dedup=global_dedup)
        task = self._make(record)
        self.tasks[task.id] = task
        task.log('任务已创建：来源 %s，目的 %d 个频道；关键词过滤 %s，屏蔽词过滤 %s，'
                 '去重 %s，日志保留 %s' % (
                     '全部频道（不含自建）' if not task.sources else '%d 个频道' % len(task.sources),
                     len(task.targets),
                     '开' if task.use_keyword else '关',
                     '开' if task.use_blocked else '关',
                     '锚点' + ('+跨任务' if task.global_dedup else '') if task.anchor_on else '关闭',
                     '永久' if not task.keep_days else '%d 天' % task.keep_days))
        if state == 'running':
            await task.start()
        return record

    async def update(self, task_id, **fields):
        record = self.store.get(task_id)
        if not record or record['kind'] != MONITOR:
            return {'ok': False, 'error': '任务不存在'}
        name = str(fields.get('name') or '').strip()
        if name:
            record['name'] = name
        cfg = dict(record.get('config') or {})
        if fields.get('sources') is not None:
            cfg['sources'] = norm_peers(fields['sources'])
        if fields.get('targets') is not None:
            targets = norm_peers(fields['targets'])
            if not targets:
                return {'ok': False, 'error': '请至少选择一个目的频道'}
            cfg['targets'] = targets
        if fields.get('keywords') is not None:
            cfg['keywords'] = clean_words(split_words(fields['keywords']))
        if fields.get('blocked') is not None:
            cfg['blocked'] = clean_words(split_words(fields['blocked']))
        for key in ('use_keyword', 'use_blocked'):
            if fields.get(key) is not None:
                cfg[key] = bool(fields[key])
        record['config'] = cfg
        if fields.get('keep_days') is not None:
            record['keep_days'] = clamp_keep_days(fields['keep_days'])
        if fields.get('anchor') is not None:
            record['anchor'] = bool(fields['anchor'])
        if fields.get('global_dedup') is not None:
            record['global_dedup'] = bool(fields['global_dedup'])
        self.store.save(record)

        task = self.ensure(task_id)
        task.apply_config()
        task.log('任务配置已更新：%s' % task_summary(record))
        if record['state'] == 'running' and not task.busy:
            await task.start()
        return {'ok': True, 'task': record}

    async def set_state(self, task_id, action):
        """暂停 / 恢复 / 立即停用。"""
        record = self.store.get(task_id)
        if record is None or record['kind'] != MONITOR:
            return {'ok': False, 'error': '任务不存在'}
        task = self.ensure(task_id)
        if action in ('pause', 'stop'):
            await task.stop(join=True)
            while not task.queue.empty():
                task.queue.get_nowait()
                task.queue.task_done()
            self.store.set_state(task_id, 'paused')
            task.log('任务已停用，不再接收新消息')
        elif action == 'resume':
            self.store.set_state(task_id, 'running')
            task.apply_config()
            await task.start()
            task.log('任务已恢复运行')
        else:
            return {'ok': False, 'error': 'action 必须是 pause 或 resume'}
        return {'ok': True, 'task': task.record}

    async def delete(self, task_id):
        task = self.tasks.get(task_id)
        if task:
            await task.stop(join=True)
        self.tasks.pop(task_id, None)
        self.store.delete(task_id)
        return {'ok': True}

    async def dispatch(self, chat, message):
        for task in self.running_tasks():
            try:
                await task.offer(chat, message)
            except Exception as exc:
                task.log('派发异常：%s' % exc)

    async def stop_all(self):
        for task in list(self.tasks.values()):
            await task.stop()


# ================= 接口 =================
def _flag(value, default=True):
    if value is None:
        return default
    return bool(value)


async def handle_monitor_create(request):
    srv = server_of(request)
    try:
        body = await request.json()
        name = str(body.get('name') or '').strip() or '实时任务'
        sources = norm_peers(body.get('sources'))
        targets = norm_peers(body.get('targets'))
        if not targets:
            return json_response({'ok': False, 'error': '请至少选择一个目的频道'}, status=400)
        record = await srv.bot.monitors.create(
            name, sources, targets,
            clean_words(split_words(body.get('keywords'))),
            clean_words(split_words(body.get('blocked'))),
            state='paused' if body.get('state') == 'paused' else 'running',
            use_keyword=_flag(body.get('use_keyword'), True),
            use_blocked=_flag(body.get('use_blocked'), True),
            keep_days=body.get('keep_days'),
            anchor=_flag(body.get('anchor'), True),
            global_dedup=_flag(body.get('global_dedup'), True))
        return json_response({'ok': True, 'task': list_public([record])[0]}, status=201)
    except Exception as exc:
        return json_response({'ok': False, 'error': str(exc)}, status=400)


async def handle_monitor_update(request):
    srv = server_of(request)
    try:
        body = await request.json()
        result = await srv.bot.monitors.update(str(body.get('id', '')), **{
            'name': body.get('name'),
            'sources': body.get('sources') if 'sources' in body else None,
            'targets': body.get('targets') if 'targets' in body else None,
            'keywords': body.get('keywords') if 'keywords' in body else None,
            'blocked': body.get('blocked') if 'blocked' in body else None,
            'use_keyword': body.get('use_keyword'),
            'use_blocked': body.get('use_blocked'),
            'keep_days': body.get('keep_days'),
            'anchor': body.get('anchor'),
            'global_dedup': body.get('global_dedup'),
        })
        if not result.get('ok'):
            return json_response(result, status=400)
        return json_response({'ok': True, 'task': list_public([result['task']])[0]})
    except Exception as exc:
        return json_response({'ok': False, 'error': str(exc)}, status=400)


async def handle_monitor_state(request):
    srv = server_of(request)
    try:
        body = await request.json()
        result = await srv.bot.monitors.set_state(
            str(body.get('id', '')), str(body.get('action', '')))
        return json_response(result, status=200 if result.get('ok') else 400)
    except Exception as exc:
        return json_response({'ok': False, 'error': str(exc)}, status=400)


ROUTES = [
    web.post('/api/tasks/monitor', handle_monitor_create),
    web.post('/api/tasks/monitor/update', handle_monitor_update),
    web.post('/api/tasks/monitor/state', handle_monitor_state),
]
