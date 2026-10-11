"""任务中心底座：任务表、独立日志、转发锚点、通用工具与任务中心接口。

- `TaskStore`：SQLite `data/tasks.db`，实时监控 / 手动转发共用一张表（`kind` 区分）；
- `TaskLog`：每条任务一个日志目录 `log/tasks/<id>/`，按自然日分文件 `<YYYY-MM-DD>.log`，
  网页端可选日期查看，也可按「日志保留天数」自动清理或手动清理；
- `AnchorStore`：**按源频道记录已转发过的消息**（锚点），防止重复转发；
- 单条任务 = 来源频道 + 目的频道 + 自己的词表 + 开关 + 去重/日志保留设置。

实时监控任务的运行时在 `monitor.py`，手动转发在 `manual.py`。
"""

import collections
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timedelta

from aiohttp import web

from core import json_response, print_ts, server_of

MONITOR = 'monitor'
MANUAL = 'manual'
KIND_LABEL = {MONITOR: '实时监控', MANUAL: '手动转发'}
KIND_SCOPE = {MONITOR: 'monitor', MANUAL: 'manual'}

STATE_LABEL = {
    'running': '运行中',
    'paused': '已停用',
    'done': '已完成',
    'cancelled': '已取消',
    'failed': '失败',
    'interrupted': '已中断',
}
FINISHED_STATES = ('done', 'cancelled', 'failed', 'interrupted')

EMPTY_STATS = {'checked': 0, 'matched': 0, 'sent': 0, 'failed': 0, 'skipped': 0}

RETENTION_DEFAULT = 30     # 默认日志保留天数
RETENTION_MAX = 3650       # 上限（约 10 年）
ANCHOR_CAP = 20000         # 单个（任务, 频道）最多记多少条锚点，超了淘汰成水位线

TS_RE = re.compile(r'^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\]')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
TASK_ID_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')


def today_text():
    """今天的日期文本 `YYYY-MM-DD`。"""
    return datetime.now().strftime('%Y-%m-%d')


# ================= 通用工具 =================
def now_ts():
    return int(time.time())


def fmt_ts(value):
    if not value:
        return ''
    return datetime.fromtimestamp(int(value)).strftime('%Y-%m-%d %H:%M:%S')


def match_any(text, words):
    """文本是否命中词表里的任一词条（不区分大小写）。"""
    if not text or not words:
        return False
    low = text.lower()
    return any(str(w).lower() in low for w in words)


def clean_words(items):
    out = []
    for x in items or []:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


def new_id(kind):
    return ('m' if kind == MONITOR else 'j') + uuid.uuid4().hex[:10]


def clamp_keep_days(value, default=RETENTION_DEFAULT):
    """日志保留天数：>0 表示保留天数上限，0 / 负数 表示永久保留。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return 0
    return min(n, RETENTION_MAX)


def norm_peers(items):
    """把前端传来的频道项（id/title 字典，或裸 id/用户名，或单个 id）归一化为 [{id,title}]。

    防御：非可迭代输入（如误传 int/None）不会抛出内部异常，而是当作空列表处理。
    """
    if items is None:
        return []
    if isinstance(items, int):
        items = [items]
    if not isinstance(items, (list, tuple, set)):
        return []
    out = []
    for item in items:
        if isinstance(item, dict):
            pid = item.get('id')
            if pid is None:
                continue
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                continue
            title = str(item.get('title') or pid)
        else:
            raw = str(item).strip()
            if not raw or not raw.lstrip('-').isdigit():
                continue
            pid = int(raw)
            title = raw
        if all(x['id'] != pid for x in out):
            out.append({'id': pid, 'title': title})
    return out


def _line_date(line):
    """从日志行里取出日期 `YYYY-MM-DD`；取不到返回 `None`（旧日志迁移用）。"""
    m = TS_RE.match(line)
    return m.group(1) if m else None


# ================= 每条任务的独立日志 =================
class TaskLog:
    """每条任务一个日志目录 `log/tasks/<id>/`，按自然日分文件 `<YYYY-MM-DD>.log`。

    - 写日志时自动落到「今天」那个文件，跨零点自动换文件；
    - `dates()` 列出该任务已有的日期，网页端据此提供「选择历史日志」的下拉框；
    - `prune()` 按「日志保留天数」整文件删除，不再逐行裁剪；
    - 旧的单文件日志 `log/tasks/<id>.log` 会在启动时由 `migrate()` 自动拆分迁移。
    """

    def __init__(self, base_dir, buffer_size=500):
        self.dir = os.path.abspath(base_dir)
        os.makedirs(self.dir, exist_ok=True)
        self.buffer_size = buffer_size
        self.buffers = {}

    # ---------------- 路径 ----------------
    @staticmethod
    def _safe(task_id):
        tid = str(task_id or '')
        return tid if TASK_ID_RE.match(tid) else None

    def folder(self, task_id):
        """该任务的日志目录 `log/tasks/<id>/`。任务 id 非法时返回 None。"""
        tid = self._safe(task_id)
        return os.path.join(self.dir, tid) if tid else None

    def legacy_path(self, task_id):
        """旧格式的单文件日志 `log/tasks/<id>.log`（迁移用）。"""
        tid = self._safe(task_id)
        return os.path.join(self.dir, '%s.log' % tid) if tid else None

    def path(self, task_id, date=None):
        """某一天的日志文件；`date` 缺省为今天。"""
        folder = self.folder(task_id)
        if not folder:
            return None
        day = date if (date and DATE_RE.match(str(date))) else today_text()
        return os.path.join(folder, '%s.log' % day)

    # ---------------- 写 ----------------
    def append(self, task_id, text, ts=True, tag=None):
        """写一行日志。`tag` 是类别标签（成功/屏蔽/未命中/失败），会插在时间戳之后，
        便于网页端按类别筛选；按当天日期落盘。"""
        text = str(text).rstrip('\n')
        day = today_text()
        if ts:
            stamp = '[%s]' % datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            line = '%s [%s] %s' % (stamp, tag, text) if tag else '%s %s' % (stamp, text)
        else:
            line = '[%s] %s' % (tag, text) if tag else text
        path = self.path(task_id, day)
        if path:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
            except Exception:
                pass
        self.buffers.setdefault(task_id, collections.deque(maxlen=self.buffer_size)).append(line)
        return line

    def recent(self, task_id):
        return list(self.buffers.get(task_id, ()))

    # ---------------- 读 ----------------
    def dates(self, task_id):
        """该任务已有的日志日期（`YYYY-MM-DD`，新的在前）。"""
        folder = self.folder(task_id)
        out = []
        if folder and os.path.isdir(folder):
            try:
                names = os.listdir(folder)
            except OSError:
                names = []
            for name in names:
                if name.endswith('.log') and DATE_RE.match(name[:-4]):
                    out.append(name[:-4])
        return sorted(out, reverse=True)

    def read(self, task_id, date=None, tail=400, max_bytes=1024 * 1024):
        """读某一天的日志（缺省今天）。返回 `lines/date/size/truncated`。"""
        day = date if (date and DATE_RE.match(str(date))) else today_text()
        path = self.path(task_id, day)
        base = {'lines': [], 'date': day, 'mtime': 0, 'size': 0, 'truncated': False}
        if not path:
            return base
        try:
            size = os.path.getsize(path)
            mtime = int(os.path.getmtime(path))
            with open(path, 'rb') as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                    text = f.read().decode('utf-8', 'ignore').split('\n', 1)[-1]
                else:
                    text = f.read().decode('utf-8', 'ignore')
            lines = text.splitlines()
            truncated = len(lines) > tail
            base.update({'lines': lines[-tail:] if truncated else lines, 'mtime': mtime,
                         'size': size, 'truncated': truncated})
            return base
        except FileNotFoundError:
            return base
        except Exception as exc:
            base['error'] = str(exc)
            return base

    # ---------------- 清理 ----------------
    def clear(self, task_id, date=None):
        """删除日志文件：给了 `date` 只删那天，否则清空该任务全部日期。返回删除行数。"""
        folder = self.folder(task_id)
        if not folder:
            return 0
        targets = self.dates(task_id) if not date else [
            date if DATE_RE.match(str(date)) else today_text()]
        dropped = 0
        for day in targets:
            path = os.path.join(folder, '%s.log' % day)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    dropped += sum(1 for _ in f)
                os.remove(path)
            except OSError:
                pass
        if not date:
            self.buffers.pop(task_id, None)
        return dropped

    def prune(self, task_id, keep_days):
        """按「日志保留天数」清理：整日期文件超出保留期就删掉。返回删除行数。"""
        if not keep_days or int(keep_days) <= 0:
            return 0
        cutoff = (datetime.now() - timedelta(days=int(keep_days))).strftime('%Y-%m-%d')
        dropped = 0
        for day in self.dates(task_id):
            if day >= cutoff:          # 文件名即日期，字典序比较等价于日期比较
                continue
            dropped += self.clear(task_id, day)
        return dropped

    def remove(self, task_id):
        """任务被删除时清掉它的整个日志目录（含旧格式单文件）。"""
        self.buffers.pop(task_id, None)
        folder = self.folder(task_id)
        if folder and os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
        legacy = self.legacy_path(task_id)
        if legacy:
            try:
                os.remove(legacy)
            except OSError:
                pass

    # ---------------- 迁移 ----------------
    def migrate(self):
        """把旧的单文件日志 `log/tasks/<id>.log` 按行内时间戳拆分到 `log/tasks/<id>/<date>.log`。"""
        moved = 0
        try:
            names = os.listdir(self.dir)
        except OSError:
            return 0
        for name in names:
            if not name.endswith('.log'):
                continue                     # 新格式是目录，不是文件
            path = os.path.join(self.dir, name)
            if not os.path.isfile(path):
                continue
            tid = name[:-4]
            if not self._safe(tid):
                continue
            try:
                moved += self._migrate_file(tid, path)
            except Exception as exc:
                print_ts('日志迁移失败 %s: %s' % (name, exc))
        if moved:
            print_ts('日志已迁移为按日分文件：共 %d 行' % moved)
        return moved

    def _migrate_file(self, task_id, path):
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.read().splitlines()
        except Exception:
            return 0
        if not lines:
            try:
                os.remove(path)
            except OSError:
                pass
            return 0
        fallback = datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d')
        buckets = collections.OrderedDict()
        last = fallback
        for ln in lines:
            m = TS_RE.match(ln)
            day = m.group(1) if m else last     # 没时间戳的续行归到上一条那天
            last = day
            buckets.setdefault(day, []).append(ln)
        n = 0
        for day, rows in buckets.items():
            target = self.path(task_id, day)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            mode = 'a' if os.path.isfile(target) else 'w'
            with open(target, mode, encoding='utf-8') as f:
                f.write('\n'.join(rows) + '\n')
            n += len(rows)
        try:
            os.remove(path)
        except OSError:
            pass
        return n


# ================= 转发锚点（按源频道去重） =================
class AnchorStore:
    """转发锚点：按「源频道」记录已经转发过的消息，防止重复转发。

    - **任务锚点**：`(task_id, chat_id) → 已转发消息 id`。任务暂停/中断后恢复、
      重启后重扫、或手动补转重复扫描同一段历史时，已转发过的消息不会再发一次。
    - **全局锚点**：`chat_id → 消息 id`，记录被任意任务转发过的源消息。
      任务勾选「跨任务去重」后，同一条源消息整体只会被转发一次。
    - 内存中按集合保存，单个（任务, 频道）超过 ANCHOR_CAP 时，把最小的 id 淘汰成
      一条「水位线」(floor)，之后 ≤ floor 的一律视为已转发 —— 内存与磁盘都不会无限增长。
    """

    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS anchors ('
                         'task_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, '
                         'updated_at INTEGER NOT NULL, PRIMARY KEY (task_id, chat_id, message_id))')
            conn.execute('CREATE TABLE IF NOT EXISTS anchor_floors ('
                         'task_id TEXT NOT NULL, chat_id INTEGER NOT NULL, floor INTEGER NOT NULL, '
                         'updated_at INTEGER NOT NULL, PRIMARY KEY (task_id, chat_id))')
            conn.execute('CREATE TABLE IF NOT EXISTS global_anchors ('
                         'chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, updated_at INTEGER NOT NULL, '
                         'PRIMARY KEY (chat_id, message_id))')
            conn.execute('CREATE TABLE IF NOT EXISTS global_floors ('
                         'chat_id INTEGER PRIMARY KEY, floor INTEGER NOT NULL, updated_at INTEGER NOT NULL)')
        self._sets = {}
        self._floors = {}
        self._globals = {}
        self._gfloors = {}
        self._load()

    def _load(self):
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            for task_id, chat_id, mid in conn.execute(
                    'SELECT task_id, chat_id, message_id FROM anchors'):
                self._sets.setdefault((task_id, int(chat_id)), set()).add(int(mid))
            for task_id, chat_id, floor in conn.execute(
                    'SELECT task_id, chat_id, floor FROM anchor_floors'):
                self._floors[(task_id, int(chat_id))] = int(floor)
            for chat_id, mid in conn.execute('SELECT chat_id, message_id FROM global_anchors'):
                self._globals.setdefault(int(chat_id), set()).add(int(mid))
            for chat_id, floor in conn.execute('SELECT chat_id, floor FROM global_floors'):
                self._gfloors[int(chat_id)] = int(floor)

    # ---------- 查询 ----------
    def seen(self, task_id, chat_id, message_id, use_global=False):
        """这条消息（该任务视角）是否已经转发过。"""
        chat_id, message_id = int(chat_id), int(message_id)
        if message_id <= self._floors.get((task_id, chat_id), 0):
            return True
        if message_id in self._sets.get((task_id, chat_id), ()):
            return True
        if use_global:
            if message_id <= self._gfloors.get(chat_id, 0):
                return True
            if message_id in self._globals.get(chat_id, ()):
                return True
        return False

    def task_info(self, task_id):
        rows = []
        for (tid, chat_id), ids in self._sets.items():
            if tid != task_id:
                continue
            floor = self._floors.get((tid, chat_id), 0)
            rows.append({'chat_id': chat_id, 'count': len(ids), 'floor': floor,
                         'max_id': max(ids) if ids else floor})
        rows.sort(key=lambda x: -x['count'])
        return {'channels': len(rows), 'messages': sum(r['count'] for r in rows),
                'items': rows[:50]}

    def summary(self):
        return {
            'tasks': len({k[0] for k in self._sets}),
            'channels': len(self._globals),
            'messages': sum(len(v) for v in self._sets.values()),
            'global_messages': sum(len(v) for v in self._globals.values()),
        }

    # ---------- 写入 ----------
    def _trim(self, s, key, floors):
        """集合超上限时淘汰最小的若干 id，返回新的水位线（没有淘汰则返回 None）。"""
        if len(s) <= ANCHOR_CAP:
            return None
        ordered = sorted(s)
        cut = len(ordered) - ANCHOR_CAP // 2
        dropped = ordered[:cut]
        for mid in dropped:
            s.discard(mid)
        floor = max(dropped[-1], floors.get(key, 0))
        floors[key] = floor
        return floor

    def advance(self, task_id, chat_id, message_id, use_global=True):
        """记录一条已成功转发的消息。返回是否产生了新记录。"""
        chat_id, message_id = int(chat_id), int(message_id)
        now = now_ts()
        key = (task_id, chat_id)
        changed = False

        s = self._sets.setdefault(key, set())
        added = message_id not in s and message_id > self._floors.get(key, 0)
        if added:
            s.add(message_id)
        floor = self._trim(s, key, self._floors)

        g_added = g_floor = None
        if use_global:
            gs = self._globals.setdefault(chat_id, set())
            if message_id not in gs and message_id > self._gfloors.get(chat_id, 0):
                gs.add(message_id)
                g_added = True
            g_floor = self._trim(gs, chat_id, self._gfloors)

        try:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                if added:
                    conn.execute('INSERT OR REPLACE INTO anchors(task_id,chat_id,message_id,updated_at)'
                                 ' VALUES(?,?,?,?)', (task_id, chat_id, message_id, now))
                    changed = True
                if floor is not None:
                    conn.execute('DELETE FROM anchors WHERE task_id=? AND chat_id=? AND message_id<=?',
                                 (task_id, chat_id, floor))
                    conn.execute('INSERT OR REPLACE INTO anchor_floors(task_id,chat_id,floor,updated_at)'
                                 ' VALUES(?,?,?,?)', (task_id, chat_id, floor, now))
                    changed = True
                if g_added:
                    conn.execute('INSERT OR REPLACE INTO global_anchors(chat_id,message_id,updated_at)'
                                 ' VALUES(?,?,?)', (chat_id, message_id, now))
                    changed = True
                if g_floor is not None:
                    conn.execute('DELETE FROM global_anchors WHERE chat_id=? AND message_id<=?',
                                 (chat_id, g_floor))
                    conn.execute('INSERT OR REPLACE INTO global_floors(chat_id,floor,updated_at)'
                                 ' VALUES(?,?,?)', (chat_id, g_floor, now))
                    changed = True
        except Exception:
            pass
        return changed

    def reset_task(self, task_id):
        """清空某条任务的锚点（重扫用它来强制重新转发）。"""
        self._sets = {k: v for k, v in self._sets.items() if k[0] != task_id}
        self._floors = {k: v for k, v in self._floors.items() if k[0] != task_id}
        try:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                conn.execute('DELETE FROM anchors WHERE task_id=?', (task_id,))
                conn.execute('DELETE FROM anchor_floors WHERE task_id=?', (task_id,))
        except Exception:
            pass
        return {'ok': True}

    def reset_global(self):
        self._globals = {}
        self._gfloors = {}
        try:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                conn.execute('DELETE FROM global_anchors')
                conn.execute('DELETE FROM global_floors')
        except Exception:
            pass
        return {'ok': True}

    def reset_all(self):
        self._sets, self._floors = {}, {}
        self.reset_global()
        try:
            with sqlite3.connect(self.db_path, timeout=15) as conn:
                conn.execute('DELETE FROM anchors')
                conn.execute('DELETE FROM anchor_floors')
        except Exception:
            pass
        return {'ok': True}


# ================= 任务表 =================
class TaskStore:
    """统一任务表（monitor / manual 共用）。"""

    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
                state TEXT NOT NULL, payload TEXT NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                finished_at INTEGER)''')
        self.cache = collections.OrderedDict()
        self._load()

    def _load(self):
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            rows = list(conn.execute(
                'SELECT id,kind,name,state,payload,created_at,updated_at,finished_at '
                'FROM tasks ORDER BY created_at DESC LIMIT 500'))
        for rid, kind, name, state, payload, created_at, updated_at, finished_at in rows:
            try:
                data = json.loads(payload)
            except (TypeError, ValueError):
                data = {}
            # 实时监控任务是长期订阅：重启后按原状态自动恢复（已停用的保持停用）；
            # 手动转发是一次性任务，重启时还在跑的直接标记为中断。
            if kind == MANUAL and state == 'running':
                state = 'interrupted'
                data.setdefault('error', '服务重启时任务被中断')
            self.cache[rid] = {
                'id': rid, 'kind': kind, 'name': name, 'state': state,
                'config': data.get('config') or {},
                'stats': dict(EMPTY_STATS, **(data.get('stats') or {})),
                'recent': data.get('recent') or [],
                'error': data.get('error', ''),
                'keep_days': clamp_keep_days(data.get('keep_days', RETENTION_DEFAULT)),
                'anchor': bool(data.get('anchor', True)),
                'global_dedup': bool(data.get('global_dedup', True)),
                'created_at': created_at, 'updated_at': updated_at,
                'finished_at': finished_at,
            }

    def save(self, task):
        task['updated_at'] = now_ts()
        payload = json.dumps({
            'config': task.get('config') or {},
            'stats': task.get('stats') or {},
            'recent': (task.get('recent') or [])[-40:],
            'error': task.get('error', ''),
            'keep_days': task.get('keep_days', RETENTION_DEFAULT),
            'anchor': bool(task.get('anchor', True)),
            'global_dedup': bool(task.get('global_dedup', True)),
        }, ensure_ascii=False)
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            conn.execute('INSERT OR REPLACE INTO tasks'
                         '(id,kind,name,state,payload,created_at,updated_at,finished_at)'
                         ' VALUES(?,?,?,?,?,?,?,?)',
                         (task['id'], task['kind'], task['name'], task['state'], payload,
                          task['created_at'], task['updated_at'], task.get('finished_at')))
        self.cache[task['id']] = task
        return task

    def get(self, task_id):
        return self.cache.get(task_id)

    def all(self, kind=None):
        items = list(self.cache.values())
        if kind:
            items = [x for x in items if x['kind'] == kind]
        items.sort(key=lambda x: x.get('updated_at') or 0, reverse=True)
        return items

    def count(self, kind=None, state=None):
        return sum(1 for x in self.all(kind) if state is None or x['state'] == state)

    def create(self, kind, name, config, state='running', recent=None,
               keep_days=RETENTION_DEFAULT, anchor=True, global_dedup=True):
        task = {
            'id': new_id(kind), 'kind': kind, 'name': name or KIND_LABEL.get(kind, kind),
            'state': state, 'config': config, 'stats': dict(EMPTY_STATS),
            'recent': list(recent or []), 'error': '',
            'keep_days': clamp_keep_days(keep_days), 'anchor': bool(anchor),
            'global_dedup': bool(global_dedup),
            'created_at': now_ts(), 'updated_at': now_ts(), 'finished_at': None,
        }
        return self.save(task)

    def delete(self, task_id):
        with sqlite3.connect(self.db_path, timeout=15) as conn:
            conn.execute('DELETE FROM tasks WHERE id=?', (task_id,))
        return self.cache.pop(task_id, None)

    def set_state(self, task_id, state, error=None):
        task = self.cache.get(task_id)
        if not task:
            return None
        task['state'] = state
        if error is not None:
            task['error'] = error
        if state in FINISHED_STATES:
            task['finished_at'] = now_ts()
        elif state in ('running', 'paused'):
            task['finished_at'] = None
        return self.save(task)

    def add_recent(self, task, text):
        task.setdefault('recent', []).append(text)
        task['recent'] = task['recent'][-40:]

    def stats_add(self, task, key, n=1):
        task.setdefault('stats', dict(EMPTY_STATS))
        task['stats'][key] = task['stats'].get(key, 0) + n


# ================= 视图 =================
def task_summary(task):
    """一句话描述任务配置，用于列表展示。"""
    cfg = task.get('config') or {}
    parts = []
    if task['kind'] == MONITOR:
        parts.append('来源：全部频道（不含自建）' if not cfg.get('sources') else '来源：%d 个频道' % len(cfg['sources']))
        parts.append('目的：%d 个频道' % len(cfg.get('targets') or []))
        kw = clean_words(cfg.get('keywords'))
        bl = clean_words(cfg.get('blocked'))
        parts.append('关键词：%s' % ('自带 %d 词' % len(kw) if kw else '用大类词表'))
        parts.append('屏蔽词：%s' % ('自带 %d 词' % len(bl) if bl else '用大类词表'))
    else:
        parts.append('来源：%d 个频道' % len(cfg.get('sources') or []))
        parts.append('目的：%d 个频道' % len(cfg.get('targets') or []))
        rng = cfg.get('range') or {}
        parts.append('时间：%s' % (
            '%s ~ %s' % (rng.get('start') or '最早', rng.get('end') or '现在')
            if (rng.get('start') or rng.get('end')) else '不限'))
        fl = cfg.get('filters') or {}
        kw = clean_words(cfg.get('keywords'))
        bl = clean_words(cfg.get('blocked'))
        parts.append('关键词：%s' % ('自带 %d 词' % len(kw) if kw
                                   else ('启用' if fl.get('keyword', True) else '未启用')))
        parts.append('屏蔽词：%s' % ('自带 %d 词' % len(bl) if bl
                                   else ('启用' if fl.get('blocked', True) else '未启用')))
    keep = task.get('keep_days', RETENTION_DEFAULT)
    parts.append('日志保留：%s' % ('永久' if not keep else '%d 天' % keep))
    if task.get('anchor', True):
        parts.append('去重：锚点%s' % (' + 跨任务' if task.get('global_dedup', True) else ''))
    else:
        parts.append('去重：关闭')
    return ' · '.join(parts)


def list_public(tasks):
    """给前端的精简视图。"""
    out = []
    for t in tasks:
        cfg = t.get('config') or {}
        out.append({
            'id': t['id'], 'kind': t['kind'], 'name': t['name'], 'state': t['state'],
            'stats': t.get('stats') or dict(EMPTY_STATS),
            'sources': len(cfg.get('sources') or []),
            'targets': len(cfg.get('targets') or []),
            'keep_days': t.get('keep_days', RETENTION_DEFAULT),
            'anchor': bool(t.get('anchor', True)),
            'global_dedup': bool(t.get('global_dedup', True)),
            'created_at': t.get('created_at'), 'updated_at': t.get('updated_at'),
            'finished_at': t.get('finished_at'),
            'created_text': fmt_ts(t.get('created_at')),
            'updated_text': fmt_ts(t.get('updated_at')),
            'error': t.get('error', ''),
            'phase': cfg.get('phase') or '',
            # 手动任务是否留有可续跑的断点（暂停/中断后点「恢复」能从断点继续，不重扫）
            'resumable': bool((cfg.get('progress') or {}).get('stage')),
            'summary': task_summary(t),
        })
    return out


# ================= 接口：任务中心 + 锚点 =================
async def handle_tasks_list(request):
    srv = server_of(request)
    store = srv.bot.task_store
    kind = request.query.get('kind')
    state = request.query.get('state')
    q = (request.query.get('q') or '').strip().lower()
    items = list_public(store.all(kind if kind in (MONITOR, MANUAL) else None))
    if state:
        items = [x for x in items if x['state'] == state]
    if q:
        items = [x for x in items if q in x['name'].lower() or q in x['id'].lower()]
    return json_response({'tasks': items, 'counts': srv.bot.task_counts()})


async def handle_task_detail(request):
    srv = server_of(request)
    task = srv.bot.task_store.get(request.query.get('id', ''))
    if not task:
        return json_response({'ok': False, 'error': '任务不存在'}, status=404)
    detail = list_public([task])[0]
    detail['config'] = task.get('config') or {}
    detail['recent'] = task.get('recent') or []
    try:
        detail['anchors'] = srv.bot.anchors.task_info(task['id'])
    except Exception:
        detail['anchors'] = {'channels': 0, 'messages': 0, 'items': []}
    date = (request.query.get('date') or '').strip()
    log = srv.bot.task_log.read(task['id'], date=date or None, tail=400)
    log['dates'] = srv.bot.task_log.dates(task['id'])
    return json_response({'ok': True, 'task': detail, 'log': log})


async def handle_task_log(request):
    srv = server_of(request)
    task_id = request.query.get('id', '')
    try:
        tail = int(request.query.get('tail', '500'))
    except (TypeError, ValueError):
        tail = 500
    tail = max(20, min(tail, 5000))
    if not srv.bot.task_store.get(task_id):
        return json_response({'ok': False, 'error': '任务不存在'}, status=404)
    date = (request.query.get('date') or '').strip()
    data = srv.bot.task_log.read(task_id, date=date or None, tail=tail)
    data['dates'] = srv.bot.task_log.dates(task_id)
    q = (request.query.get('q') or '').strip()
    if q:
        data['lines'] = [ln for ln in data['lines'] if q in ln]
        data['filtered'] = True
    data['ok'] = True
    data['q'] = q
    return json_response(data)


async def handle_task_log_clear(request):
    """清理任务日志。

    body: `{'id': <任务 id 或 '*'>, 'date': 'YYYY-MM-DD' 或留空}`。
    - `date` 留空 → 清空该任务的全部日期；
    - `id` 为 `*` → 清空所有任务的日志（可配合 `date` 只清某一天）。
    """
    srv = server_of(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    task_id = str(body.get('id') or '')
    date = str(body.get('date') or '').strip()
    if date and not DATE_RE.match(date):
        return json_response({'ok': False, 'error': '日期格式应为 YYYY-MM-DD'}, status=400)
    log = srv.bot.task_log
    if task_id == '*':
        dropped = 0
        for task in srv.bot.task_store.all():
            dropped += log.clear(task['id'], date or None)
        return json_response({'ok': True, 'scope': 'all', 'date': date, 'cleared': dropped})
    if not srv.bot.task_store.get(task_id):
        return json_response({'ok': False, 'error': '任务不存在'}, status=404)
    dropped = log.clear(task_id, date or None)
    if not date:
        # 清空全部后补一行说明，避免页面看起来「日志凭空消失」
        log.append(task_id, '该任务的历史日志已被清空')
    return json_response({'ok': True, 'scope': 'task', 'date': date, 'cleared': dropped})


async def handle_task_delete(request):
    srv = server_of(request)
    try:
        body = await request.json()
        task_id = str(body.get('id', ''))
        task = srv.bot.task_store.get(task_id)
        if not task:
            return json_response({'ok': False, 'error': '任务不存在'}, status=404)
        if task['kind'] == MONITOR:
            await srv.bot.monitors.delete(task_id)
        else:
            await srv.manual.cancel_and_drop(task_id)
        srv.bot.task_log.remove(task_id)
        srv.bot.anchors.reset_task(task_id)
        return json_response({'ok': True})
    except Exception as exc:
        return json_response({'ok': False, 'error': str(exc)}, status=400)


async def handle_anchors_get(request):
    srv = server_of(request)
    task_id = request.query.get('id', '')
    if task_id:
        if not srv.bot.task_store.get(task_id):
            return json_response({'ok': False, 'error': '任务不存在'}, status=404)
        data = srv.bot.anchors.task_info(task_id)
    else:
        data = srv.bot.anchors.summary()
    data['ok'] = True
    return json_response(data)


async def handle_anchors_reset(request):
    srv = server_of(request)
    try:
        body = await request.json()
        scope = str(body.get('scope') or 'task')
        task_id = str(body.get('id') or '')
        if scope == 'all':
            srv.bot.anchors.reset_all()
        elif scope == 'global':
            srv.bot.anchors.reset_global()
        else:
            if not srv.bot.task_store.get(task_id):
                return json_response({'ok': False, 'error': '任务不存在'}, status=404)
            srv.bot.anchors.reset_task(task_id)
        return json_response({'ok': True, 'scope': scope})
    except Exception as exc:
        return json_response({'ok': False, 'error': str(exc)}, status=400)


ROUTES = [
    web.get('/api/tasks', handle_tasks_list),
    web.get('/api/tasks/detail', handle_task_detail),
    web.get('/api/tasks/log', handle_task_log),
    web.post('/api/tasks/log/clear', handle_task_log_clear),
    web.post('/api/tasks/delete', handle_task_delete),
    web.get('/api/anchors', handle_anchors_get),
    web.post('/api/anchors/reset', handle_anchors_reset),
]
