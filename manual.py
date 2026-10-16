"""手动（一次性）转发任务。

手动选择来源频道与目的频道，指定起止时间范围，扫描并转发命中的消息。
每条任务都是独立的 asyncio 任务，可以**同时并行多条**，进度、统计与日志
（`log/tasks/<id>/<日期>.log`）都写在统一任务中心里。

支持：
- **暂停 / 恢复（按进度续跑）**：暂停时把「扫描到哪了 + 待转发队列 + 转发到第几条」落盘，
  恢复时从断点继续——扫描阶段接着扫、转发阶段直接续转队列，**不会重新扫描一遍**；
  服务重启导致协程消失后，点「恢复」同样按断点续跑（没有断点才回退为按锚点重扫）；
- **编辑**：未在运行的任务可以改来源/目的/时间/关键词/日志保留等（改了扫描相关项会丢弃断点）；
- **锚点去重**：按源频道记录已转发过的消息，重扫与跨任务都不会重复。

本模块同时提供 `/api/tasks/manual*` 接口。
"""

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from functools import partial

from aiohttp import web

from core import (TAG_BLOCK, TAG_FAIL, TAG_OK_DIRECT, TAG_OK_DOWNLOAD, TAG_SKIP, err_text,
                  id_range_text, json_response, preview_text, print_ts, server_of, split_words)
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


def _lower_words(value):
    """把任务自带的关键词/屏蔽词统一成小写字符串列表。"""
    return [str(x).strip().lower() for x in (value or []) if str(x).strip()]


class _WordFeed:
    """任务的词表来源，支持热重载。

    - **任务自带**词表（创建/编辑时填了关键词或屏蔽词）→ 固定快照，运行期间不变；
    - **留空** → 跟随「手动转发」大类全局词表：词表一改，正在跑的任务下一条消息
      就用上新词表。靠 KeywordManager 的版本号判断，没变时零拷贝零分配。

    `fixed` 传空列表即表示「用全局词表」。`on_change` 在运行中检测到词表变化时回调。
    """

    def __init__(self, store, scope, kind, fixed=None, on_change=None):
        self.store = store
        self.scope = scope
        self.kind = kind
        self.fixed = list(fixed or [])
        self.on_change = on_change
        self._words = None
        self._version = None

    @property
    def local(self):
        """是否使用任务自带词表（自带时不做热重载）。"""
        return bool(self.fixed)

    async def get(self):
        if self.fixed:
            return self.fixed
        version, words = await self.store.words_versioned(self.scope, self.kind)
        if version != self._version:
            first = self._version is None      # 首次取值不算「变更」，不写日志
            self._version = version
            self._words = words                # 只读引用，重载时整体换新对象
            if not first and self.on_change:
                await self.on_change(self.kind, list(words))
        return self._words or []


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
        self._shutdown = False       # 服务关闭中：此时被取消的任务记为「已停用」而非「已取消」
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

    # ---------------- 续跑进度（断点）持久化 ----------------
    # 「恢复」要按进度续跑，就不能把进度只放在内存里：
    #   阶段一扫到的消息 → 逐条追加到 log/tasks/<id>.queue（JSONL，一行 [来源频道 id, 消息 id]）；
    #   扫描断点 / 转发游标 → 写进任务记录的 config.progress（很小的 dict，随任务一起落库）。
    # 这样即便进程被杀掉，重启后点「恢复」也能接着上次的位置继续。
    def _queue_path(self, task_id):
        return os.path.join(BASE_DIR, 'log', 'tasks', '%s.queue' % task_id)

    def _queue_append(self, task_id, chat_id, mid):
        path = self._queue_path(task_id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps([chat_id, mid]) + '\n')
        except Exception as exc:
            print_ts('队列落盘失败 %s: %s' % (task_id, exc))

    def _queue_read(self, task_id):
        out = []
        try:
            with open(self._queue_path(task_id), 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, list) and len(item) == 2:
                        out.append((item[0], item[1]))
        except FileNotFoundError:
            pass
        except Exception as exc:
            print_ts('读取队列失败 %s: %s' % (task_id, exc))
        return out

    def _queue_drop(self, task_id):
        try:
            os.remove(self._queue_path(task_id))
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _queue_iter(self, task_id):
        """流式逐行读队列文件（不整份读进内存，百万条也不占 RAM）。"""
        try:
            with open(self._queue_path(task_id), 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, list) and len(item) == 2:
                        yield (item[0], item[1])
        except FileNotFoundError:
            return

    def _queue_count(self, task_id):
        n = 0
        for _ in self._queue_iter(task_id):
            n += 1
        return n

    # ---------------- 待补发清单 ----------------
    # 转发最终失败（超过重试上限）的条目记到 <id>.retry，任务跑完后自动补发几轮；
    # 仍失败的保留在文件里，日志里能看到，之后「恢复 / 重新开始」会再处理。
    def _retry_path(self, task_id):
        return os.path.join(BASE_DIR, 'log', 'tasks', '%s.retry' % task_id)

    def _retry_append(self, task_id, chat_id, mid):
        path = self._retry_path(task_id)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'a', encoding='utf-8') as f:
                f.write(json.dumps([chat_id, mid]) + '\n')
        except Exception as exc:
            print_ts('待补发清单落盘失败 %s: %s' % (task_id, exc))

    def _retry_read(self, task_id):
        out = []
        try:
            with open(self._retry_path(task_id), 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, list) and len(item) == 2:
                        out.append((item[0], item[1]))
        except FileNotFoundError:
            pass
        except Exception as exc:
            print_ts('读取待补发清单失败 %s: %s' % (task_id, exc))
        return out

    def _retry_count(self, task_id):
        return len(self._retry_read(task_id))

    def _retry_remove(self, task_id, chat_id, mid):
        items = [x for x in self._retry_read(task_id) if list(x) != [chat_id, mid]]
        path = self._retry_path(task_id)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                for x in items:
                    f.write(json.dumps(list(x)) + '\n')
        except Exception as exc:
            print_ts('更新待补发清单失败 %s: %s' % (task_id, exc))

    def _retry_drop(self, task_id):
        try:
            os.remove(self._retry_path(task_id))
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def progress_of(self, task_id):
        """该任务已保存的续跑断点（没有则返回空 dict）。"""
        record = self.store.get(task_id)
        if not record:
            return {}
        return dict((record.get('config') or {}).get('progress') or {})

    def _drop_progress(self, task_id):
        """丢弃续跑断点：删队列文件 + 清掉记录里的 progress。"""
        self._queue_drop(task_id)
        record = self.store.get(task_id)
        if not record:
            return
        cfg = record.get('config')
        if cfg and cfg.pop('progress', None) is not None:
            self.store.save(record)

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
        await self._event(record, '已恢复，从当前进度继续')

    # ---------------- 创建 / 编辑 ----------------
    @staticmethod
    def _range_cfg(start, end):
        return {
            'start': start.astimezone().strftime('%Y-%m-%d %H:%M') if start else '',
            'end': end.astimezone().strftime('%Y-%m-%d %H:%M') if end else '',
        }

    async def start(self, sources, targets, start_date=None, end_date=None,
                    keywords=None, blocked=None, limit=5000, name=None, filter_keyword=True,
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
            'blocked': [str(x).strip() for x in _as_word_list(blocked) if str(x).strip()][:50],
            'limit': limit,
            'filters': {'keyword': bool(filter_keyword), 'blocked': bool(filter_blocked)},
        }, keep_days=clamp_keep_days(keep_days), anchor=anchor, global_dedup=global_dedup)
        self.log.append(record['id'], '任务已创建：%s' % task_summary(record))
        await self._spawn(record)
        return list_public([record])[0]

    async def _spawn(self, record, ignore_global=False, resume=False):
        """按任务当前配置解析频道并启动协程。

        `ignore_global=True` 时本次运行忽略「跨任务去重」全局锚点（供「重新开始」强制重发）。
        `resume=True` 表示「接着上次的断点继续跑」（读 config.progress + 队列文件）；
        新建 / 重新开始 / 编辑后重跑都走 `resume=False`，会顺手丢掉旧的断点。
        """
        cfg = record.get('config') or {}
        if not resume:
            self._queue_drop(record['id'])
            self._retry_drop(record['id'])
            cfg.pop('progress', None)
        # 无论哪种方式启动，都先把「自动恢复」标记清掉——已经跑起来了，
        # 之后若再次被关闭，会在 _run 的收尾逻辑里按当时的情况重新决定是否打标记。
        record.setdefault('config', cfg).pop('auto_resume', None)
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
        # 关键词/屏蔽词：任务自带的固定不变；留空的跟随「手动转发」大类全局词表，
        # 任务运行中在「词表管理」改动会即时热重载（见 _WordFeed）。
        kw_feed = _WordFeed(self.bot.word_store, MANUAL_SCOPE, 'keyword',
                            _lower_words(cfg.get('keywords')),
                            on_change=partial(self._on_word_change, record))
        bl_feed = _WordFeed(self.bot.word_store, MANUAL_SCOPE, 'blocked',
                            _lower_words(cfg.get('blocked')),
                            on_change=partial(self._on_word_change, record))
        # 开了关键词过滤、或任务自带关键词 → 需要做关键词判断
        filter_on = kw_on or kw_feed.local
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
                                            kw_feed, bl_feed, filter_on, bl_on, limit,
                                            use_global=(bool(record.get('global_dedup', True))
                                                        and not ignore_global),
                                            resume=resume))
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
        if fields.get('blocked') is not None:
            cfg['blocked'] = [str(x).strip() for x in _as_word_list(fields['blocked'])
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
        # 改了「影响扫描范围/过滤条件」的配置 → 旧断点（扫到哪、队列里有什么）已不适用，丢掉
        scan_fields = ('sources', 'targets', 'start_datetime', 'end_datetime',
                       'keywords', 'blocked', 'limit', 'filter_keyword', 'filter_blocked')
        dropped_progress = False
        if any(k in fields for k in scan_fields) and cfg.pop('progress', None) is not None:
            self._queue_drop(task_id)
            dropped_progress = True
        self.store.save(record)
        self.log.append(task_id, '任务配置已更新：%s' % task_summary(record))
        if dropped_progress:
            self.log.append(task_id, '配置已变更，之前保存的扫描进度已作废；下次「恢复」会按新配置重新扫描')
        return {'ok': True, 'task': record}

    # ---------------- 任务控制 ----------------
    async def pause(self, task_id):
        record = self.store.get(task_id)
        if not record or record['kind'] != MANUAL:
            return {'ok': False, 'error': '任务不存在'}
        if record['state'] != 'running':
            return {'ok': False, 'error': '只有运行中的任务可以暂停'}
        self._gate(task_id).clear()
        # 用户主动暂停 → 明确不代表「重启后要自动恢复」，清掉标记
        (record.get('config') or {}).pop('auto_resume', None)
        self.store.set_state(task_id, 'paused')
        prog = self.progress_of(task_id)
        where = ''
        if prog:
            stage = '转发' if prog.get('stage') == 'forward' else '扫描'
            where = '（当前在%s阶段，已完成 %d 条，已记录断点）' % (stage, int(prog.get('pos') or 0))
        self.log.append(task_id, '任务已暂停：当前这条处理完就停下%s；点「恢复」会从这里接着跑，'
                                 '不会重新扫描' % where)
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
            self.log.append(task_id, '任务已恢复，从当前进度继续')
            return {'ok': True}
        # 协程已经不在（服务重启 / 被中断）：按上次保存的断点接着跑，而不是从头重扫
        prog = self.progress_of(task_id)
        if prog:
            stage = '转发' if prog.get('stage') == 'forward' else '扫描'
            self.log.append(task_id, '任务从上次的断点继续（%s阶段，已完成 %d 条），'
                                     '不会重复扫描已处理的部分' % (stage, int(prog.get('pos') or 0)))
        else:
            self.log.append(task_id, '没有可用的断点，按原配置重新扫描（已转发过的消息会按锚点自动跳过）')
        try:
            await self._spawn(record, resume=True)
        except Exception as exc:
            self.store.set_state(task_id, 'failed', error=str(exc))
            self.log.append(task_id, '恢复失败：%s' % exc, tag=TAG_FAIL)
            return {'ok': False, 'error': str(exc)}
        return {'ok': True}

    async def auto_resume_all(self):
        """服务启动时自动恢复「上次没跑完」的手动转发任务。

        两类会被自动拉起，它们都代表「用户本来就希望它继续跑」：
          - state == 'interrupted'：进程被硬杀（kill -9 / 断电 / 异常退出）时任务正在运行；
          - state == 'paused' 且 config.auto_resume：优雅关闭（systemctl restart）时留下的，
            关闭那一刻已打好标记（见 _run 的 CancelledError 分支）。
        用户自己点过「暂停」的任务不带该标记，因此不会被自动拉起，避免误启动。
        """
        if not getattr(self.bot, 'telegram_authorized', False):
            print_ts('Telegram 尚未登录，暂不自动恢复手动转发任务；登录成功后会再试一次。')
            return 0
        pending = []
        for record in self.store.all(MANUAL):
            cfg = record.get('config') or {}
            if record['state'] == 'interrupted' or (
                    record['state'] == 'paused' and cfg.get('auto_resume')):
                pending.append(record['id'])
        if not pending:
            return 0
        print_ts('检测到 %d 个未跑完的手动转发任务，正在自动恢复…' % len(pending))
        for task_id in pending:
            try:
                result = await self.resume(task_id)
                if not result.get('ok'):
                    self.log.append(task_id, '自动恢复失败：%s' % result.get('error'), tag=TAG_FAIL)
            except Exception as exc:
                self.log.append(task_id, '自动恢复出错：%s' % err_text(exc), tag=TAG_FAIL)
        return len(pending)

    async def restart(self, task_id):
        """重新开始：清空该任务进度（锚点 + 统计），从头重新扫描并转发。

        与「恢复」不同——恢复会按锚点跳过已转发过的消息；「重新开始」会先清空**该任务**的
        锚点，并且本次运行忽略「跨任务去重」全局锚点，因此时间范围内命中的消息会全部
        **重新转发一遍**（已转过的也会再发，可能产生重复）。运行中的任务会先被取消再重开。
        """
        record = self.store.get(task_id)
        if not record or record['kind'] != MANUAL:
            return {'ok': False, 'error': '任务不存在'}
        # 1) 正在运行就先停掉当前协程（不等它自然结束）
        task = self._tasks.get(task_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.pop(task_id, None)
        self._pause.pop(task_id, None)
        # 2) 清空进度：本任务锚点 + 统计 + 近期日志 + 续跑断点（全局锚点不动，避免影响其它任务）
        self.bot.anchors.reset_task(task_id)
        self._drop_progress(task_id)
        record['stats'] = dict(EMPTY_STATS)
        record['recent'] = []
        record['error'] = ''
        self.store.set_state(task_id, 'running')
        self.log.append(task_id, '任务已重新开始：已清空该任务的转发锚点与统计，将从头重新扫描并转发')
        # 3) 重新扫描转发（忽略跨任务去重锚点，确保重发）
        try:
            await self._spawn(record, ignore_global=True)
        except Exception as exc:
            self.store.set_state(task_id, 'failed', error=str(exc))
            self.log.append(task_id, '重新开始失败：%s' % exc, tag=TAG_FAIL)
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
        self._queue_drop(task_id)
        self.store.delete(task_id)
        return {'ok': True}

    async def stop_all(self):
        """服务关闭时调用：把正在跑的手动任务停在「已停用」，而不是记成「已取消」。

        这样重启后任务仍在列表里可以点「恢复」继续 —— 恢复会按原配置重新扫描，
        已经转发过的消息由锚点自动跳过，不会重复发。硬杀进程（kill -9 / 断电）走不到这里，
        那种情况由 TaskStore._load 标记为「已中断」，同样是可恢复的。
        """
        self._shutdown = True
        running = [t for t in self._tasks.values() if t is not None and not t.done()]
        for t in running:
            t.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self._tasks.clear()

    def running_count(self):
        return sum(1 for t in self._tasks.values() if not t.done())

    # ---------------- 执行 ----------------
    async def _event(self, record, text, tag=None):
        line = self.log.append(record['id'], text, tag=tag)
        self.store.add_recent(record, line)
        return line

    async def _on_word_change(self, record, kind, words):
        """任务运行期间「手动转发」全局词表被改动 → 写一行日志告知已换用新词表。"""
        label = '关键词' if kind == 'keyword' else '屏蔽词'
        await self._event(record, '检测到「手动转发」大类%s已更新（共 %d 条），'
                                  '后续消息按新词表过滤' % (label, len(words)))

    async def _start_anchor_id(self, record, chat, start):
        """定位「起始时间」之前最近的一条消息 id，作为正向扫描的起点。

        为什么不用 `iter_messages(reverse=True, offset_date=start)`：
        实测 Telethon 1.40 在反向模式下 offset_date 语义异常（会直接跳到频道末尾），
        而 `offset_id` 在反向模式下语义明确——从该 id **之后**开始，按「旧 → 新」遍历。
        找不到锚点（频道里没有早于起始时间的消息）时返回 None，表示从频道最旧一条开始扫。
        """
        try:
            got = await self.bot.client.get_messages(chat, limit=1, offset_date=start)
        except Exception as exc:
            await self._event(record, '起始时间定位失败（改从频道最旧开始扫）：%s' % exc,
                              TAG_FAIL)
            return None
        if not got:
            return None
        return getattr(got[0], 'id', None)

    @staticmethod
    def _dest_label(record, dests):
        """目的频道的可读名，用于日志：优先用实时解析出的实体标题，回退任务配置里的标题。"""
        names = []
        for d in (dests or []):
            name = getattr(d, 'title', None) or getattr(d, 'username', None)
            if not name:
                name = str(getattr(d, 'id', '') or '')
            if name:
                names.append(str(name))
        if names:
            return '、'.join(names)
        cfg_titles = [str(x.get('title')) for x in (record.get('config', {}).get('targets') or [])
                      if x.get('title')]
        return '、'.join(cfg_titles) if cfg_titles else '默认目的'

    async def _fetch_message(self, record, chat, mid, max_tries=30):
        """取回待转发的消息。

        网络类异常会重试（退避最长 60 秒），最多 max_tries 次；真正取不到（返回 None，
        通常是被删除或无权限）直接返回 None，交给外层按失败跳过。
        """
        attempt = 0
        while True:
            try:
                msg = await self.bot.client.get_messages(chat, ids=mid)
                if msg is not None:
                    return msg
                return None                      # 消息确实不存在了
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                attempt += 1
                if attempt >= max_tries:
                    await self._event(record, '消息 %s 取回失败已达 %d 次，放弃：%s'
                                      % (mid, attempt, err_text(exc)), TAG_FAIL)
                    return None
                delay = min(3 * attempt, 60)
                if attempt <= 5 or attempt % 10 == 0:
                    await self._event(record, '消息 %s 取回失败（第 %d 次重试，%d 秒后再试）：%s'
                                      % (mid, attempt, delay, err_text(exc)), TAG_FAIL)
                await self._wait_if_paused(record)
                await asyncio.sleep(delay)

    async def _backfill(self, record, dests, chat_by_id, anchor_on, dest_label, rounds=3):
        """把本轮转发失败（记入 `<id>.retry`）的条目再补发几轮。

        每轮之间隔 30 秒；成功的条目即时从清单移除。跑完仍失败的保留在文件里，
        日志会说明，之后点「恢复」或「重新开始」会再处理。
        """
        task_id = record['id']
        for round_no in range(1, rounds + 1):
            items = self._retry_read(task_id)
            if not items:
                return
            await self._event(record, '待补发 %d 条，开始第 %d/%d 轮补发'
                              % (len(items), round_no, rounds))
            for cid, mid in items:
                await self._wait_if_paused(record)
                chat = chat_by_id.get(cid)
                if chat is None:
                    self._retry_remove(task_id, cid, mid)
                    continue
                message = await self._fetch_message(record, chat, mid)
                if message is None:
                    self._retry_remove(task_id, cid, mid)
                    await self._event(record, '消息 %s 已不可用，从待补发清单移除' % mid, TAG_SKIP)
                    continue
                ok = await self.bot.deliver(
                    chat, message, dests,
                    log=lambda s, tag=None: self.log.append(task_id, s, tag=tag),
                    retry_forever=True,
                    on_sent=lambda ids, gid: None,
                    gate=lambda: self._wait_if_paused(record),
                    lock=self._send_lock)
                if ok and ok != 'empty':
                    record['stats']['sent'] += 1
                    self.bot.stats.inc('forwarded', scope=_SCOPE)
                    self.bot.last_forward_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    if anchor_on:
                        self.bot.anchors.advance(task_id, self.bot.peer_id(chat), mid)
                    if ok == 'download':
                        how, tag = '补发 · 下载后转发成功', TAG_OK_DOWNLOAD
                    else:
                        how, tag = '补发 · 转发成功', TAG_OK_DIRECT
                    await self._event(record, '消息 %s %s → %s（%s）' % (
                        mid, how, dest_label, getattr(chat, 'title', chat.id)), tag)
                    self._retry_remove(task_id, cid, mid)
                elif ok == 'empty':
                    self._retry_remove(task_id, cid, mid)
                else:
                    await self._event(record, '消息 %s 补发第 %d 轮仍失败' % (mid, round_no),
                                      TAG_FAIL)
                self.store.save(record)
                await asyncio.sleep(self.bot.forward_interval)
            await asyncio.sleep(30)
        left = self._retry_count(task_id)
        if left:
            await self._event(record, '补发结束：仍有 %d 条未成功，已保留在待补发清单；'
                                      '之后点「恢复」或「重新开始」会再处理' % left)

    async def _run(self, record, chats, dests, start, end, kw_feed, bl_feed, filter_on,
                   bl_on, limit, use_global=True, resume=False):
        task_id = record['id']
        anchor_on = bool(record.get('anchor', True))
        target_ids = {self.bot.peer_id(x) for x in dests}
        chat_by_id = {self.bot.peer_id(x): x for x in chats}
        # 断点：扫描到哪个来源 / 该来源扫到哪条 / 转发到队列第几条。直接挂在任务 config 上，
        # 随 store.save() 一起落库；队列本体在 log/tasks/<id>.queue。
        progress = record.setdefault('config', {}).setdefault('progress', {})
        stage = 'scan'
        src_from = 0
        pos_done = 0
        # 阶段一收集的条目**只写队列文件**（逐条落盘），内存里仅保留计数：
        # 大频道一次扫上百万条也不会把内存吃光（容器总共才 2GB）。
        queued = 0
        seen_groups = set()           # 本次扫描已入队的相册 grouped_id（同组只入队第一条）
        usable_break = False             # 是否真的读到了可用的断点（否则就是从零重扫）
        if resume:
            stage = progress.get('stage') if progress.get('stage') in ('scan', 'forward') else 'scan'
            src_from = max(0, int(progress.get('src') or 0))
            pos_done = max(0, int(progress.get('pos') or 0))
            queued = self._queue_count(task_id)
            if not queued and stage == 'forward':
                stage, src_from, pos_done = 'scan', 0, 0     # 队列文件没了，只能退回重扫
            if stage == 'scan':
                pos_done = 0
                usable_break = queued > 0 or src_from > 0 or bool(progress.get('anchor'))
            else:
                usable_break = True
        anchor_skipped = 0
        skipped_other = 0
        try:
            if resume and not usable_break:
                # 没有可用断点 → 只能从零重扫。扫描类计数会被重新累加，先归零避免叠加；
                # 「转发成功/失败」是每条消息的真实结果、重扫时被锚点跳过不会重算，所以保留。
                for key in ('checked', 'matched', 'skipped'):
                    record['stats'][key] = 0
                # 队列文件里若有旧残留，从零重扫前先丢掉，避免重复入队
                self._queue_drop(task_id)
                queued = 0
                await self._event(record, '没有可用断点，从零重新扫描；本任务的「已扫描/命中/跳过」已归零'
                                          '（不叠加，转发成功/失败计数保留），已转发过的消息会按锚点自动跳过')
            if resume and stage == 'forward':
                # ---------- 续跑：队列还在，直接接着转发，完全跳过扫描 ----------
                record['config']['phase'] = 'forwarding'
                await self._event(record, '从断点继续：队列共 %d 条，已完成 %d 条，'
                                          '直接接着转发（不重新扫描）' % (queued, pos_done))
            else:
                # ---------- 阶段一：全量扫描，只收集入队，一条都不发 ----------
                record['config']['phase'] = 'scanning'
                record['stats']['queued'] = queued
                if resume and queued:
                    await self._event(record, '从断点继续扫描：队列已有 %d 条，从第 %d 个来源接着扫'
                                      % (queued, min(src_from + 1, len(chats))))
                await self._event(record, '开始扫描（此阶段只收集待转发消息，暂不发送）')
                for si in range(src_from, len(chats)):
                    chat = chats[si]
                    chat_id = self.bot.peer_id(chat)
                    title = getattr(chat, 'title', str(chat_id))
                    record['config']['current_source'] = title
                    await self._event(record, '开始扫描：%s' % title)
                    seg_ids = []                # 本频道这次入队的消息 id（只留少量供日志汇总）
                    seg_count = 0
                    # 从「起始时间」往后扫（旧 → 新）：先按起始时间定位锚点，再从锚点之后正向遍历。
                    # 续扫时当前来源直接用上次存下的断点 id（没有起始时间时也一样），不回退重扫。
                    if si == src_from and progress.get('anchor'):
                        anchor_id = progress.get('anchor')
                    else:
                        anchor_id = await self._start_anchor_id(record, chat, start) if start else None
                    iter_kwargs = {'limit': limit, 'reverse': True}
                    if anchor_id:
                        iter_kwargs['offset_id'] = anchor_id
                    await self._event(record, '扫描方向：旧 → 新%s' % (
                        '（自 %s 起）' % start.astimezone().strftime('%Y-%m-%d %H:%M')
                        if start else '（从频道最旧一条开始）'))
                    async for message in self.bot.client.iter_messages(chat, **iter_kwargs):
                        await self._wait_if_paused(record)
                        progress['stage'] = 'scan'
                        progress['src'] = si
                        progress['anchor'] = message.id
                        record['stats']['checked'] += 1
                        if record['stats']['checked'] % 20 == 0:
                            self.store.save(record)
                        # 正向扫描：越过结束时间就可以停了；还没到起始时间的先跳过
                        if end and message.date > end:
                            break
                        if start and message.date < start:
                            continue
                        if anchor_on and self.bot.anchors.seen(task_id, chat_id, message.id, use_global):
                            record['stats']['skipped'] += 1
                            self.bot.stats.inc('skipped', scope=_SCOPE)
                            anchor_skipped += 1
                            if anchor_skipped <= 20 or anchor_skipped % 50 == 0:
                                await self._event(record, '消息 %s 已转发过（锚点），跳过' % message.id,
                                                  TAG_SKIP)
                            continue
                        text = message.message or ''
                        if filter_on:
                            words = await kw_feed.get()      # 留空时每次取最新（热重载）
                            if not any(word in text.lower() for word in words):
                                continue
                            record['stats']['matched'] += 1
                            self.bot.stats.inc('matched', scope=_SCOPE)
                        blocked = await bl_feed.get() if bl_on else ()
                        if any(word in text.lower() for word in blocked):
                            record['stats']['skipped'] += 1
                            self.bot.stats.inc('skipped', scope=_SCOPE)
                            skipped_other += 1
                            if skipped_other <= 20:
                                await self._event(record, '消息 %s 命中屏蔽词，跳过：%s'
                                                  % (message.id, preview_text(text)), TAG_BLOCK)
                            continue
                        if chat_id in target_ids:
                            record['stats']['skipped'] += 1
                            self.bot.stats.inc('skipped', scope=_SCOPE)
                            skipped_other += 1
                            if skipped_other <= 20:
                                await self._event(record, '消息 %s 来源是目标频道，跳过' % message.id,
                                                  TAG_SKIP)
                            continue
                        if not self.bot._has_content(message):
                            # 服务消息（频道创建/置顶…）与空消息：无内容可转，不入队
                            record['stats']['skipped'] += 1
                            self.bot.stats.inc('skipped', scope=_SCOPE)
                            skipped_other += 1
                            if skipped_other <= 20:
                                await self._event(record, '消息 %s 无文字也无媒体（服务消息），跳过'
                                                  % message.id, TAG_SKIP)
                            continue
                        gid = getattr(message, 'grouped_id', None)
                        if gid is not None:
                            # 相册只入队第一条：转发时 deliver 会整组发送（含本条后面
                            # 的各条成员），后续成员再入队只会把整组重复发几遍。
                            if gid in seen_groups:
                                continue
                            seen_groups.add(gid)
                        # 只落盘、不进内存：断点续跑与转发阶段都从队列文件流式读
                        self._queue_append(task_id, chat_id, message.id)
                        queued += 1
                        seg_count += 1
                        if len(seg_ids) < 200:
                            seg_ids.append(message.id)
                        record['stats']['queued'] = queued
                        if queued % 20 == 0:
                            self.store.save(record)
                    if seg_count:                                    # 入队只汇总一行，不逐条刷屏
                        more = '' if seg_count == len(seg_ids) else (' 等 %d 条' % seg_count)
                        await self._event(record, '%s%s 进入队列（本频道入队 %d 条，累计待转发 %d 条）'
                                          % (id_range_text(seg_ids), more, seg_count, queued))
                    record['config']['sources_done'] = record['config'].get('sources_done', 0) + 1
                    self.store.save(record)

                # 阶段一扫完（或本来就在转发阶段）→ 记下阶段二断点，之后按「队列第几条」续跑
                progress['stage'] = 'forward'
                progress['pos'] = pos_done
                progress['queued_total'] = queued
                progress.pop('src', None)
                progress.pop('anchor', None)

            # ---------- 阶段二：从队列文件里逐条取出发送（流式读，不占内存） ----------
            total = int(progress.get('queued_total') or 0) or self._queue_count(task_id)
            self.store.save(record)
            record['config']['phase'] = 'forwarding'
            record['config']['current_source'] = ''
            if stage == 'forward':
                pass                                     # 续跑时上面已经写过日志了
            elif total:
                await self._event(record, '扫描完成：待转发 %d 条（跳过已转发/重复/未命中 %d 条），将按「旧 → 新」逐条转发'
                                  % (total, anchor_skipped + skipped_other))
            else:
                await self._event(record, '扫描完成：没有需要转发的消息（跳过已转发/重复/未命中 %d 条）'
                                  % (anchor_skipped + skipped_other))
            sent_ok = 0
            dest_label = self._dest_label(record, dests)
            for index, (cid, mid) in enumerate(self._queue_iter(task_id), 1):
                if index <= pos_done:                    # 续跑：这些上次已经处理过了
                    continue
                chat = chat_by_id.get(cid)
                if chat is None:
                    record['stats']['failed'] += 1
                    self.bot.stats.inc('failed', scope=_SCOPE)
                    await self._event(record, '消息 %s 的来源频道已不在任务配置中，跳过' % mid,
                                      TAG_FAIL)
                    progress['pos'] = index
                    record['stats']['queued'] = max(0, total - index)
                    self.store.save(record)
                    continue
                await self._wait_if_paused(record)
                message = await self._fetch_message(record, chat, mid)
                if message is None:
                    record['stats']['failed'] += 1
                    self.bot.stats.inc('failed', scope=_SCOPE)
                    await self._event(record, '消息 %s 已不可用（可能已删除），跳过' % mid, TAG_FAIL)
                else:
                    await self._event(record, '消息 %s 开始转发（%d/%d）' % (mid, index, total))
                    group_ids = []       # 相册整组发送成功后回调填进来的本组全部 id
                    ok = await self.bot.deliver(
                        chat, message, dests,
                        log=lambda s, tag=None: self.log.append(task_id, s, tag=tag),
                        retry_forever=True,
                        on_sent=lambda ids, gid: group_ids.extend(ids),
                        gate=lambda: self._wait_if_paused(record),
                        lock=self._send_lock)
                    if ok == 'empty':
                        # 服务消息 / 空消息：无内容可转。不算成功也不算失败，跳过即可
                        record['stats']['skipped'] += 1
                        self.bot.stats.inc('skipped', scope=_SCOPE)
                        skipped_other += 1
                        await self._event(record, '消息 %s 无文字也无媒体（服务消息），跳过' % mid,
                                          TAG_SKIP)
                    elif ok:
                        sent_ok += 1
                        record['stats']['sent'] += 1
                        self.bot.stats.inc('forwarded', scope=_SCOPE)
                        self.bot.last_forward_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        if anchor_on:
                            self.bot.anchors.advance(task_id, self.bot.peer_id(chat), mid)
                            # 相册：把同组其它成员也推进锚点，断线补扫/续跑时不会重发
                            for gid_mid in group_ids:
                                self.bot.anchors.advance(task_id, self.bot.peer_id(chat), gid_mid)
                        # 按实际转发路径分两类日志：直接转发 / 下载后转发（见 bot.deliver 返回值）
                        if ok == 'download':
                            how, tag = '下载后转发成功', TAG_OK_DOWNLOAD
                        else:
                            how, tag = '转发成功', TAG_OK_DIRECT
                        await self._event(record, '消息 %s %s → %s（%s）：%s' % (
                            mid, how, dest_label, getattr(chat, 'title', chat.id),
                            preview_text(message.message)), tag)
                    else:
                        record['stats']['failed'] += 1
                        self.bot.stats.inc('failed', scope=_SCOPE)
                        # 记入待补发清单：任务跑完后自动补发几轮，仍失败的保留可手动恢复
                        self._retry_append(task_id, cid, mid)
                        await self._event(record, '消息 %s 转发失败，已记入待补发清单' % mid, TAG_FAIL)
                record['stats']['queued'] = max(0, total - index)
                progress['pos'] = index               # 断点：本条已处理完，恢复时从这里之后继续
                self.store.save(record)
                await asyncio.sleep(self.bot.forward_interval)
            # ---------- 阶段三：把本轮失败的条目自动补发几轮，再算任务完成 ----------
            await self._backfill(record, dests, chat_by_id, anchor_on, dest_label)
            record['state'] = 'done'
            record['config']['phase'] = ''
            record['config']['current_source'] = ''
            record['config'].pop('progress', None)     # 跑完了，断点与队列文件都不再需要
            self._queue_drop(task_id)
            await self._event(record, '手动转发完成：成功 %d 条，跳过 %d 条已转发过的消息'
                              % (sent_ok, anchor_skipped))
        except asyncio.CancelledError:
            if self._shutdown:
                # 服务关闭导致的中断：停在「已停用」；若关闭前任务仍在运行（不是用户手动暂停的），
                # 打上 auto_resume 标记，进程重启后由 auto_resume_all() 自动接着跑（断点保留）。
                was_paused = record['state'] == 'paused'     # 用户先点了「暂停」再关服务 → 不自动恢复
                record['state'] = 'paused'
                record['error'] = ''
                record['config']['phase'] = ''
                if not was_paused:
                    record['config']['auto_resume'] = True
                record['stats']['queued'] = max(0, self._queue_count(task_id)
                                                - int(progress.get('pos') or 0))
                await self._event(record, '服务关闭，任务已停止（断点已保留%s）' % (
                    '，重启后会自动恢复' if not was_paused else '，等待手动恢复'))
            else:
                record['state'] = 'cancelled'
                await self._event(record, '任务已取消（断点已保留，点「恢复」可从当前位置继续）')
        except Exception as exc:
            record['state'] = 'failed'
            record['error'] = str(exc)
            await self._event(record, '任务失败：%s' % exc, TAG_FAIL)
        finally:
            if record['state'] == 'running':
                record['state'] = 'done'
            if record['state'] in ('done', 'cancelled', 'failed'):
                record['finished_at'] = int(time.time())
                if record['state'] != 'cancelled':
                    record['stats']['queued'] = 0
                record['config']['phase'] = ''
            if record['state'] == 'done':
                record['config'].pop('progress', None)
                self._queue_drop(task_id)
            # 只有「服务关闭留下的已停用」才保留自动恢复标记；其余终态一律清掉，
            # 免得用户手动取消/任务失败后又莫名其妙自己跑起来。
            if record['state'] != 'paused':
                record['config'].pop('auto_resume', None)
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
            blocked=body.get('blocked'),
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
            'name', 'sources', 'targets', 'start_datetime', 'end_datetime', 'keywords', 'blocked',
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
        elif action == 'restart':
            result = await srv.manual.restart(task_id)
        else:
            result = {'ok': False, 'error': 'action 必须是 pause / resume / restart'}
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
