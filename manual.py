"""手动（一次性）转发任务。

手动选择来源频道与目的频道，指定起止时间范围，扫描并转发命中的消息。
每条任务都是独立的 asyncio 任务，可以**同时并行多条**，进度、统计与日志
（`log/tasks/<id>.log`）都写在统一任务中心里。

支持：
- **暂停 / 恢复**：暂停后停在当前消息之后，恢复继续（服务重启后恢复会按原配置重扫，
  已经转发过的消息由「锚点」自动跳过，不会重复转发）；
- **编辑**：未在运行的任务可以改来源/目的/时间/关键词/日志保留等；
- **锚点去重**：按源频道记录已转发过的消息，重扫与跨任务都不会重复。

本模块同时提供 `/api/tasks/manual*` 接口。
"""

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

from aiohttp import web

from core import json_response, server_of, split_words
from tasks import (MANUAL, EMPTY_STATS, KIND_SCOPE, clamp_keep_days, fmt_ts,
                   list_public, norm_peers, task_summary)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_SCOPE = KIND_SCOPE[MANUAL]
MANUAL_SCOPE = 'manual'


def _as_word_list(value):
    """关键词/屏蔽词归一化为字符串列表；支持换行或 ;；,，、 分隔（与词表预览格式一致）。"""
    return split_words(value)


def _flag(value, default=True):
    if value is None:
        return default
    return bool(value)


class ManualForward:
    # 支持的时间格式：前端 datetime-local 传 '%Y-%m-%dT%H:%M'，也兼容旧版纯日期
    _DT_FORMATS = ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S',
                   '%Y-%m-%d %H:%M', '%Y-%m-%d')

    def __init__(self, bot):
        self.bot = bot
        self.store = bot.task_store
        self.log = bot.task_log
        self._tasks = {}
        self._pause = {}
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._migrate_legacy()

    # ---------------- 旧数据迁移 ----------------
    def _migrate_legacy(self):
        """把旧版 oneshot_jobs.db 的历史任务导入统一任务表（只做一次）。"""
        path = os.path.join(BASE_DIR, 'data', 'oneshot_jobs.db')
        if not os.path.isfile(path) or self.store.count(MANUAL):
            return
        imported = 0
        try:
            with sqlite3.connect(path) as conn:
                rows = list(conn.execute(
                    'SELECT id,state,payload,updated_at FROM jobs ORDER BY updated_at DESC LIMIT 100'))
            for job_id, state, payload, updated_at in rows:
                try:
                    data = json.loads(payload)
                except (TypeError, ValueError):
                    continue
                if self.store.get(job_id):
                    continue
                state = 'interrupted' if state == 'running' else state
                record = {
                    'id': job_id, 'kind': MANUAL,
                    'name': data.get('name') or ('手动转发 %s' % job_id),
                    'state': state,
                    'config': {
                        'sources': data.get('sources') or [],
                        'targets': data.get('targets') or [],
                        'range': data.get('range') or {},
                        'keywords': data.get('keywords') or [],
                        'limit': data.get('limit') or 0,
                        'filters': data.get('filters') or {},
                    },
                    'stats': dict(EMPTY_STATS, **{
                        'checked': data.get('checked', 0), 'matched': data.get('matched', 0),
                        'sent': data.get('sent', 0), 'failed': data.get('failed', 0),
                        'skipped': data.get('skipped', 0)}),
                    'recent': data.get('recent') or [],
                    'error': data.get('error', ''),
                    'keep_days': 30, 'anchor': True, 'global_dedup': True,
                    'created_at': int(data.get('started_at') or updated_at),
                    'updated_at': int(updated_at),
                    'finished_at': data.get('finished_at'),
                }
                self.store.save(record)
                imported += 1
        except Exception as exc:
            self._print('旧手动任务迁移失败: %s' % exc)
            return
        if imported:
            self._print('已从旧任务库导入 %d 条历史手动任务' % imported)

    @staticmethod
    def _print(text):
        from core import print_ts
        print_ts(text)

    # ---------------- 时间解析 ----------------
    @classmethod
    def _bound(cls, value, end=False):
        """把本地时区的时间字符串解析为 UTC 的 tz-aware datetime。"""
        if not value:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        parsed = None
        used = None
        for fmt in cls._DT_FORMATS:
            try:
                parsed = datetime.strptime(raw, fmt)
                used = fmt
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError('无法解析时间：%s（示例 2026-10-11T19:30）' % raw)
        if used == '%Y-%m-%d' and end:
            parsed = parsed.replace(hour=23, minute=59, second=59)
        return parsed.astimezone(timezone.utc)

    # ---------------- 暂停控制 ----------------
    def _gate(self, task_id):
        ev = self._pause.get(task_id)
        if ev is None:
            ev = asyncio.Event()
            ev.set()
            self._pause[task_id] = ev
        return ev

    async def _wait_if_paused(self, record):
        ev = self._gate(record['id'])
        if record['state'] == 'running' and ev.is_set():
            return
        ev.clear()
        await self._event(record, '任务已暂停，等待恢复…')
        await ev.wait()
        await self._event(record, '任务已恢复，继续扫描')

    # ---------------- 创建 / 编辑 ----------------
    @staticmethod
    def _range_cfg(start, end):
        return {
            'start': start.astimezone().strftime('%Y-%m-%d %H:%M') if start else '',
            'end': end.astimezone().strftime('%Y-%m-%d %H:%M') if end else '',
        }

    async def start(self, sources, targets, start_date=None, end_date=None,
                    keywords=None, limit=5000, name=None, filter_keyword=True,
                    filter_blocked=True, keep_days=None, anchor=True, global_dedup=True):
        src_meta = norm_peers(sources)
        dst_meta = norm_peers(targets)
        if not src_meta or not dst_meta:
            raise ValueError('来源频道和目的频道都不能为空')
        start = self._bound(start_date)
        end = self._bound(end_date, True)
        if start and end and start > end:
            raise ValueError('起始时间不能晚于结束时间')
        limit = max(1, min(int(limit or 5000), 10000))

        name = (name or '').strip() or ('手动转发 %s' % datetime.now().strftime('%m-%d %H:%M'))
        record = self.store.create(MANUAL, name, {
            'sources': src_meta,
            'targets': dst_meta,
            'range': self._range_cfg(start, end),
            'keywords': [str(x).strip() for x in _as_word_list(keywords) if str(x).strip()][:50],
            'limit': limit,
            'filters': {'keyword': bool(filter_keyword), 'blocked': bool(filter_blocked)},
        }, keep_days=clamp_keep_days(keep_days), anchor=anchor, global_dedup=global_dedup)
        self.log.append(record['id'], '任务已创建：%s' % task_summary(record))
        await self._spawn(record)
        return list_public([record])[0]

    async def _spawn(self, record):
        """按任务当前配置解析频道并启动协程。"""
        cfg = record.get('config') or {}
        chats = await self.bot.resolve_peers([x['id'] for x in (cfg.get('sources') or [])])
        dests = await self.bot.resolve_peers([x['id'] for x in (cfg.get('targets') or [])])
        if not chats:
            raise ValueError('来源频道解析失败，请检查所选频道')
        if not dests:
            raise ValueError('目的频道解析失败，请检查所选频道')
        start = self._bound((cfg.get('range') or {}).get('start'))
        end = self._bound((cfg.get('range') or {}).get('end'), True)
        fl = cfg.get('filters') or {}
        kw_on = bool(fl.get('keyword', True))
        bl_on = bool(fl.get('blocked', True))
        words = [str(x).strip().lower() for x in (cfg.get('keywords') or []) if str(x).strip()]
        if not words and kw_on:
            words = [x.lower() for x in await self.bot.word_store.words(MANUAL_SCOPE, 'keyword')]
        filter_on = bool(words) or kw_on
        blocked = ([x.lower() for x in await self.bot.word_store.words(MANUAL_SCOPE, 'blocked')]
                   if bl_on else [])
        limit = max(1, min(int(cfg.get('limit') or 5000), 10000))

        self.store.set_state(record['id'], 'running')
        self.log.append(record['id'], '开始扫描：来源 %d 个频道，目的 %d 个频道；关键词过滤 %s，'
                                      '屏蔽词过滤 %s，去重 %s' % (
                                          len(cfg.get('sources') or []), len(cfg.get('targets') or []),
                                          '开' if filter_on else '关', '开' if bl_on else '关',
                                          '锚点' + ('+跨任务' if record.get('global_dedup', True) else '')
                                          if record.get('anchor', True) else '关闭'))
        self._gate(record['id']).set()
        task = asyncio.create_task(self._run(record, chats, dests, start, end,
                                            words, blocked, filter_on, limit))
        self._tasks[record['id']] = task
        return task

    async def update(self, task_id, **fields):
        record = self.store.get(task_id)
        if not record or record['kind'] != MANUAL:
            return {'ok': False, 'error': '任务不存在'}
        if record['state'] == 'running':
            return {'ok': False, 'error': '任务正在运行，请先暂停后再编辑'}
        name = str(fields.get('name') or '').strip()
        if name:
            record['name'] = name
        cfg = dict(record.get('config') or {})
        if fields.get('sources') is not None:
            src = norm_peers(fields['sources'])
            if not src:
                return {'ok': False, 'error': '来源频道不能为空'}
            cfg['sources'] = src
        if fields.get('targets') is not None:
            dst = norm_peers(fields['targets'])
            if not dst:
                return {'ok': False, 'error': '目的频道不能为空'}
            cfg['targets'] = dst
        start = self._bound(fields.get('start_datetime')) if 'start_datetime' in fields else None
        end = self._bound(fields.get('end_datetime'), True) if 'end_datetime' in fields else None
        if 'start_datetime' in fields or 'end_datetime' in fields:
            if not fields.get('start_datetime'):
                start = None
            if not fields.get('end_datetime'):
                end = None
            if start and end and start > end:
                return {'ok': False, 'error': '起始时间不能晚于结束时间'}
            cfg['range'] = self._range_cfg(start, end)
        if fields.get('keywords') is not None:
            cfg['keywords'] = [str(x).strip() for x in _as_word_list(fields['keywords'])
                               if str(x).strip()][:50]
        if fields.get('limit') is not None:
            cfg['limit'] = max(1, min(int(fields['limit'] or 5000), 10000))
        fl = dict(cfg.get('filters') or {})
        if fields.get('filter_keyword') is not None:
            fl['keyword'] = bool(fields['filter_keyword'])
        if fields.get('filter_blocked') is not None:
            fl['blocked'] = bool(fields['filter_blocked'])
        cfg['filters'] = fl
        record['config'] = cfg
        if fields.get('keep_days') is not None:
            record['keep_days'] = clamp_keep_days(fields['keep_days'])
        if fields.get('anchor') is not None:
            record['anchor'] = bool(fields['anchor'])
        if fields.get('global_dedup') is not None:
            record['global_dedup'] = bool(fields['global_dedup'])
        self.store.save(record)
        self.log.append(task_id, '任务配置已更新：%s' % task_summary(record))
        return {'ok': True, 'task': record}

    # ---------------- 任务控制 ----------------
    async def pause(self, task_id):
        record = self.store.get(task_id)
        if not record or record['kind'] != MANUAL:
            return {'ok': False, 'error': '任务不存在'}
        if record['state'] != 'running':
            return {'ok': False, 'error': '只有运行中的任务可以暂停'}
        self._gate(task_id).clear()
        self.store.set_state(task_id, 'paused')
        self.log.append(task_id, '任务已暂停（当前这条转发完就停下，可随时恢复）')
        return {'ok': True}

    async def resume(self, task_id):
        record = self.store.get(task_id)
        if not record or record['kind'] != MANUAL:
            return {'ok': False, 'error': '任务不存在'}
        if record['state'] not in ('paused', 'interrupted', 'failed', 'cancelled'):
            return {'ok': False, 'error': '该任务当前无需恢复'}
        task = self._tasks.get(task_id)
        if task is not None and not task.done():
            self.store.set_state(task_id, 'running')
            self._gate(task_id).set()
            self.log.append(task_id, '任务已恢复')
            return {'ok': True}
        # 协程已经不在（服务重启 / 之前被中断）：按原配置重新扫描一遍，
        # 已经转发过的消息会由「锚点」自动跳过，不会重复转发。
        self.log.append(task_id, '任务重新开始扫描（已转发过的消息会按锚点自动跳过）')
        try:
            await self._spawn(record)
        except Exception as exc:
            self.store.set_state(task_id, 'failed', error=str(exc))
            self.log.append(task_id, '恢复失败：%s' % exc)
            return {'ok': False, 'error': str(exc)}
        return {'ok': True}

    async def cancel(self, task_id):
        task = self._tasks.get(task_id)
        record = self.store.get(task_id)
        if not record or record['kind'] != MANUAL:
            return {'ok': False, 'error': '任务不存在'}
        if task is None or task.done():
            return {'ok': False, 'error': '任务已结束，无法取消'}
        task.cancel()
        return {'ok': True}

    async def cancel_and_drop(self, task_id):
        task = self._tasks.get(task_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.pop(task_id, None)
        self._pause.pop(task_id, None)
        self.store.delete(task_id)
        return {'ok': True}

    def running_count(self):
        return sum(1 for t in self._tasks.values() if not t.done())

    # ---------------- 执行 ----------------
    async def _event(self, record, text):
        line = self.log.append(record['id'], text)
        self.store.add_recent(record, line)
        return line

    async def _run(self, record, chats, dests, start, end, words, blocked, filter_on, limit):
        task_id = record['id']
        anchor_on = bool(record.get('anchor', True))
        use_global = bool(record.get('global_dedup', True))
        target_ids = {self.bot.peer_id(x) for x in dests}
        seen = set()
        anchor_skipped = 0
        try:
            for chat in chats:
                chat_id = self.bot.peer_id(chat)
                title = getattr(chat, 'title', str(chat_id))
                record['config']['current_source'] = title
                await self._event(record, '开始扫描：%s' % title)
                async for message in self.bot.client.iter_messages(chat, limit=limit):
                    await self._wait_if_paused(record)
                    record['stats']['checked'] += 1
                    if record['stats']['checked'] % 20 == 0:
                        self.store.save(record)
                    if start and message.date < start:
                        break
                    if end and message.date > end:
                        continue
                    key = (chat_id, message.id)
                    if key in seen:
                        record['stats']['skipped'] += 1
                        continue
                    seen.add(key)
                    if anchor_on and self.bot.anchors.seen(task_id, chat_id, message.id, use_global):
                        record['stats']['skipped'] += 1
                        self.bot.stats.inc('skipped', scope=_SCOPE)
                        anchor_skipped += 1
                        if anchor_skipped <= 20 or anchor_skipped % 50 == 0:
                            await self._event(record, '消息 %s 已转发过（锚点），跳过' % message.id)
                        continue
                    text = message.message or ''
                    if filter_on:
                        if not any(word in text.lower() for word in words):
                            continue
                        record['stats']['matched'] += 1
                        self.bot.stats.inc('matched', scope=_SCOPE)
                    if any(word in text.lower() for word in blocked):
                        record['stats']['skipped'] += 1
                        self.bot.stats.inc('skipped', scope=_SCOPE)
                        await self._event(record, '消息 %s 命中屏蔽词，跳过' % message.id)
                        continue
                    if chat_id in target_ids:
                        record['stats']['skipped'] += 1
                        self.bot.stats.inc('skipped', scope=_SCOPE)
                        await self._event(record, '消息 %s 来源是目标频道，跳过' % message.id)
                        continue
                    async with self._send_lock:
                        await self._event(record, '消息 %s 开始转发' % message.id)
                        ok = await self.bot.deliver(
                            chat, message, dests,
                            log=lambda s: self.log.append(task_id, s))
                    if ok:
                        record['stats']['sent'] += 1
                        self.bot.stats.inc('forwarded', scope=_SCOPE)
                        self.bot.last_forward_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        if anchor_on:
                            self.bot.anchors.advance(task_id, chat_id, message.id)
                        await self._event(record, '消息 %s 转发成功' % message.id)
                    else:
                        record['stats']['failed'] += 1
                        self.bot.stats.inc('failed', scope=_SCOPE)
                    await asyncio.sleep(self.bot.forward_interval)
                record['config']['sources_done'] = record['config'].get('sources_done', 0) + 1
            record['state'] = 'done'
            record['config']['current_source'] = ''
            await self._event(record, '手动转发完成（共跳过 %d 条已转发过的消息）' % anchor_skipped)
        except asyncio.CancelledError:
            record['state'] = 'cancelled'
            await self._event(record, '任务已取消')
        except Exception as exc:
            record['state'] = 'failed'
            record['error'] = str(exc)
            await self._event(record, '任务失败：%s' % exc)
        finally:
            if record['state'] == 'running':
                record['state'] = 'done'
            if record['state'] in ('done', 'cancelled', 'failed'):
                record['finished_at'] = int(time.time())
            self.store.save(record)
            self._tasks.pop(task_id, None)

    # ---------------- 查询 ----------------
    def status(self, task_id=None):
        if task_id:
            record = self.store.get(task_id)
            return list_public([record])[0] if record else {'error': '任务不存在'}
        return list_public(self.store.all(MANUAL))


# ================= 接口 =================
async def handle_manual_start(request):
    srv = server_of(request)
    try:
        body = await request.json()
        result = await srv.manual.start(
            sources=body.get('sources', []),
            targets=body.get('targets', []),
            start_date=body.get('start_datetime') or body.get('start_date'),
            end_date=body.get('end_datetime') or body.get('end_date'),
            keywords=body.get('keywords'),
            limit=body.get('limit', 5000),
            name=body.get('name'),
            filter_keyword=_flag(body.get('filter_keyword'), True),
            filter_blocked=_flag(body.get('filter_blocked'), True),
            keep_days=body.get('keep_days'),
            anchor=_flag(body.get('anchor'), True),
            global_dedup=_flag(body.get('global_dedup'), True))
        return json_response({'ok': True, 'task': result}, status=202)
    except Exception as exc:
        return json_response({'ok': False, 'error': str(exc)}, status=400)


async def handle_manual_update(request):
    srv = server_of(request)
    try:
        body = await request.json()
        fields = {k: body[k] for k in (
            'name', 'sources', 'targets', 'start_datetime', 'end_datetime', 'keywords',
            'limit', 'filter_keyword', 'filter_blocked', 'keep_days', 'anchor', 'global_dedup')
            if k in body}
        result = await srv.manual.update(str(body.get('id', '')), **fields)
        if not result.get('ok'):
            return json_response(result, status=400)
        return json_response({'ok': True, 'task': list_public([result['task']])[0]})
    except Exception as exc:
        return json_response({'ok': False, 'error': str(exc)}, status=400)


async def handle_manual_state(request):
    srv = server_of(request)
    try:
        body = await request.json()
        task_id = str(body.get('id', ''))
        action = str(body.get('action', ''))
        if action in ('pause', 'stop'):
            result = await srv.manual.pause(task_id)
        elif action == 'resume':
            result = await srv.manual.resume(task_id)
        else:
            result = {'ok': False, 'error': 'action 必须是 pause 或 resume'}
        return json_response(result, status=200 if result.get('ok') else 400)
    except Exception as exc:
        return json_response({'ok': False, 'error': str(exc)}, status=400)


async def handle_manual_cancel(request):
    srv = server_of(request)
    try:
        body = await request.json()
        result = await srv.manual.cancel(body.get('id', ''))
        return json_response(result, status=200 if result.get('ok') else 400)
    except Exception as exc:
        return json_response({'ok': False, 'error': str(exc)}, status=400)


ROUTES = [
    web.post('/api/tasks/manual', handle_manual_start),
    web.post('/api/tasks/manual/update', handle_manual_update),
    web.post('/api/tasks/manual/state', handle_manual_state),
    web.post('/api/tasks/manual/cancel', handle_manual_cancel),
]
