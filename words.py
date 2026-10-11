"""词表：实时监控与手动转发**各用各的**，没有总表。

- 文件：`word/keyword.<scope>.txt` / `word/blocked.<scope>.txt`，scope ∈ {monitor, manual}；
- 每个大类两个开关：`use_keyword`（启用关键词过滤）、`use_blocked`（启用屏蔽词过滤）；
- 两者都启用时**屏蔽词优先**：命中关键词但同时命中屏蔽词 → 不转发；
- 单条任务可以自带关键词/屏蔽词，自带时以任务自己的为准。

本模块同时提供 `/api/words*` 接口。
"""

import asyncio
import json
import logging
import os
from datetime import datetime

from aiohttp import web

from core import json_response, print_ts, server_of

WORD_SCOPES = ('monitor', 'manual')
WORD_SCOPE_LABEL = {'monitor': '实时监控', 'manual': '手动转发'}


WORD_COMMENT_PREFIX = '##'
WORD_FILE_HEADER = (
    '## 词表文件：每行一个词条，空行忽略；以 ## 开头的整行是注释。\n'
    '## 单个 # 开头视为普通词条（例如 #tag）。可在网页「词表管理」批量维护。\n'
)


def dedup_words(items):
    seen = set()
    out = []
    for x in items:
        k = str(x).strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def parse_word_lines(text):
    """解析词表文本：每行一个词条，空行忽略，`##` 开头的整行是注释。

    与 KeywordManager.load_keywords() 使用**同一套规则**，避免「网页里看到的」
    与「实际用于过滤的」不一致。兼容历史遗留的 `\\#` 转义（表示词条以 `#` 开头），
    行首的 `\\#` 会被还原为 `#`。
    """
    out = []
    for line in str(text).splitlines():
        t = line.strip()
        if not t or t.startswith(WORD_COMMENT_PREFIX):
            continue
        if t.startswith('\\#'):
            t = t[1:]
        out.append(t)
    return out


def word_file_text(words):
    """把词条列表序列化为词表文件内容（带自述注释头，读回时会被忽略）。"""
    body = '\n'.join(words)
    return WORD_FILE_HEADER + (body + '\n' if body else '')


class KeywordManager:
    """单个词表文件，按 mtime 热加载。"""

    def __init__(self, file_path):
        self.file_path = file_path
        self.keywords = []
        self.last_update = None
        self.version = 0            # 每次重载 +1，供长跑任务判断「词表是否变了」而无需重读
        self._lock = asyncio.Lock()
        self._last_mtime = 0

    async def reload_periodically(self, interval=60):
        while True:
            try:
                mtime = os.path.getmtime(self.file_path)
                if mtime != self._last_mtime:
                    await self.reload_keywords()
                    self._last_mtime = mtime
            except FileNotFoundError:
                pass
            except Exception as exc:
                logging.error('词表热加载失败 %s: %s', self.file_path, exc)
            await asyncio.sleep(interval)

    async def reload_keywords(self):
        new_keywords = self.load_keywords()
        async with self._lock:
            self.keywords = new_keywords
            self.version += 1
            self.last_update = datetime.now()
            logging.info('列表已更新: 共 %d 个，更新时间: %s', len(self.keywords),
                         self.last_update.strftime('%Y-%m-%d %H:%M:%S'))

    def load_keywords(self):
        if not os.path.isfile(self.file_path):
            print_ts("词表文件 '%s' 不存在，使用空列表。" % self.file_path)
            return []
        with open(self.file_path, 'r', encoding='utf-8') as f:
            # 与网页读取共用 parse_word_lines：跳过空行/`##` 注释，统一处理 `\#`
            return dedup_words(parse_word_lines(f.read()))

    async def snapshot(self):
        async with self._lock:
            return self.keywords.copy()

    async def versioned(self):
        """返回 (版本号, 词条列表)。列表是**只读引用**——重载时整个换新对象，
        不会原地修改，所以调用方可以放心长期持有。用于长跑任务按版本号判断是否需要换词表。"""
        async with self._lock:
            return self.version, self.keywords


class WordScopeStore:
    def __init__(self, base_dir, seeds=None):
        self.base_dir = base_dir
        self.seed_words = seeds or {}
        data_dir = os.path.join(base_dir, 'data')
        os.makedirs(data_dir, exist_ok=True)
        # 词表统一放在 word/ 目录下（首次运行会自动把根目录的旧词表迁移进来）
        self.word_dir = os.path.join(base_dir, 'word')
        os.makedirs(self.word_dir, exist_ok=True)
        self.config_path = os.path.join(data_dir, 'word_scopes.json')
        self.migration_flag = os.path.join(data_dir, 'words_migrated_v2')
        self.config = self._load_config()
        self._migrate_legacy_files()
        first_run = not os.path.exists(self.migration_flag)
        self.managers = {}
        for scope in WORD_SCOPES:
            for kind in ('keyword', 'blocked'):
                path = self.path(scope, kind)
                if first_run:
                    self._seed_if_blank(path, kind)
                self.managers[(scope, kind)] = KeywordManager(path)
        if first_run:
            try:
                with open(self.migration_flag, 'w', encoding='utf-8') as f:
                    f.write(datetime.now().isoformat())
            except Exception:
                pass

    def _migrate_legacy_files(self):
        """把旧版散落在项目根目录的 keyword.<scope>.txt / blocked.<scope>.txt 迁到 word/。"""
        moved = 0
        for scope in WORD_SCOPES:
            for kind in ('keyword', 'blocked'):
                old = os.path.join(self.base_dir, '%s.%s.txt' % (kind, scope))
                new = self.path(scope, kind)
                if os.path.isfile(old) and not os.path.exists(new):
                    try:
                        os.replace(old, new)
                        moved += 1
                    except Exception as exc:
                        print_ts('词表迁移失败 %s -> %s: %s' % (old, new, exc))
        if moved:
            print_ts('已将 %d 个词表文件迁移到 word/ 目录' % moved)

    # ---------------- 文件与开关 ----------------
    def path(self, scope, kind):
        return os.path.join(self.word_dir, '%s.%s.txt' % (kind, scope))

    def manager(self, scope, kind):
        return self.managers[(scope, kind)]

    def managers_list(self):
        return list(self.managers.values())

    def _seed_if_blank(self, path, kind):
        """首次迁移时用原有词表做初始内容，避免丢词（只跑一次）。"""
        words = [str(w).strip() for w in self.seed_words.get(kind) or [] if str(w).strip()]
        if not words:
            return
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    # 只有「有效词条」才算非空；仅含 ## 注释头的模板仍视为空，允许播种
                    if parse_word_lines(f.read()):
                        return
            except Exception:
                return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(word_file_text(words))
            print_ts('已用原有词表初始化 %s：共 %d 条' % (os.path.basename(path), len(words)))
        except Exception as exc:
            print_ts('初始化 %s 失败: %s' % (os.path.basename(path), exc))

    @staticmethod
    def _kind_key(kind):
        return 'use_blocked' if kind == 'blocked' else 'use_keyword'

    @staticmethod
    def _defaults():
        return {s: {'use_keyword': True, 'use_blocked': True} for s in WORD_SCOPES}

    def _load_config(self):
        cfg = self._defaults()
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                for s in WORD_SCOPES:
                    item = data.get(s) or {}
                    cfg[s]['use_keyword'] = bool(item.get('use_keyword', True))
                    cfg[s]['use_blocked'] = bool(item.get('use_blocked', True))
        except FileNotFoundError:
            pass
        except Exception as exc:
            print_ts('读取词表开关配置失败，使用默认值: %s' % exc)
        self.save_config(cfg)
        return cfg

    def save_config(self, cfg=None):
        cfg = self.config if cfg is None else cfg
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            print_ts('保存词表开关配置失败: %s' % exc)

    def snapshot(self):
        return {s: dict(self.config.get(s) or {}) for s in WORD_SCOPES}

    def update(self, scope, use_keyword=None, use_blocked=None):
        if scope not in WORD_SCOPES:
            raise ValueError('未知作用域：%s' % scope)
        item = self.config.setdefault(scope, dict(self._defaults()[scope]))
        for key, val in (('use_keyword', use_keyword), ('use_blocked', use_blocked)):
            if val is not None:
                item[key] = bool(val)
        self.save_config()
        return self.snapshot()

    def kind_enabled(self, scope, kind):
        """该大类的某类过滤是否启用；未启用表示不做该类判断。"""
        cfg = self.config.get(scope) or {}
        return bool(cfg.get(self._kind_key(kind), True))

    # ---------------- 读取词条 ----------------
    async def words(self, scope, kind):
        if scope not in WORD_SCOPES:
            scope = WORD_SCOPES[0]
        kind = 'blocked' if kind == 'blocked' else 'keyword'
        return dedup_words(await self.managers[(scope, kind)].snapshot())

    async def words_versioned(self, scope, kind):
        """带版本号的词条读取，供长跑任务做「变了才更新」的热重载。"""
        if scope not in WORD_SCOPES:
            scope = WORD_SCOPES[0]
        kind = 'blocked' if kind == 'blocked' else 'keyword'
        return await self.managers[(scope, kind)].versioned()

    async def effective_keywords(self, scope):
        return await self.words(scope, 'keyword')

    async def effective_blocked(self, scope):
        return await self.words(scope, 'blocked')


# ================= 接口 =================
def _scope_of(value):
    return value if value in WORD_SCOPES else WORD_SCOPES[0]


async def handle_words_get(request):
    srv = server_of(request)
    scope = _scope_of(request.query.get('scope', 'monitor'))
    qtype = 'blocked' if request.query.get('type') == 'blocked' else 'keyword'
    path = srv.bot.word_store.path(scope, qtype)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            items = parse_word_lines(f.read())
    except Exception:
        items = []
    return json_response({'type': qtype, 'scope': scope, 'items': items})


async def handle_words_post(request):
    srv = server_of(request)
    try:
        body = await request.json()
        qtype = 'blocked' if body.get('type') == 'blocked' else 'keyword'
        scope = _scope_of(body.get('scope', 'monitor'))
        action = body.get('action', '')
        words = body.get('words', [])
        if not isinstance(words, list) or not words:
            return json_response({'ok': False, 'error': 'words 必须是非空数组'}, status=400)
        words = [str(w).strip() for w in words if str(w).strip()]
        if not words:
            return json_response({'ok': False, 'error': '没有有效词条'}, status=400)
        if any('\n' in w or '\r' in w for w in words):
            return json_response({'ok': False, 'error': '词条不能包含换行'}, status=400)
        if action not in ('add', 'remove'):
            return json_response({'ok': False, 'error': 'action 必须是 add 或 remove'}, status=400)
    except Exception as exc:
        return json_response({'ok': False, 'error': str(exc)}, status=400)

    store = srv.bot.word_store
    path = store.path(scope, qtype)
    mgr = store.manager(scope, qtype)
    try:
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                current = parse_word_lines(f.read())
        else:
            current = []
        if action == 'add':
            for w in words:
                if w not in current:
                    current.append(w)
        else:
            drop = set(words)
            current = [w for w in current if w not in drop]
        with open(path, 'w', encoding='utf-8') as f:
            f.write(word_file_text(current))
        await mgr.reload_keywords()
    except Exception as exc:
        return json_response({'ok': False, 'error': str(exc)}, status=500)
    return json_response({'ok': True, 'type': qtype, 'scope': scope, 'count': len(current)})


async def handle_word_scopes_get(request):
    srv = server_of(request)
    store = srv.bot.word_store
    counts = {}
    for scope in WORD_SCOPES:
        counts[scope] = {
            'keyword': len(await store.words(scope, 'keyword')),
            'blocked': len(await store.words(scope, 'blocked')),
        }
    return json_response({'scopes': store.snapshot(), 'counts': counts})


async def handle_word_scopes_post(request):
    srv = server_of(request)
    try:
        body = await request.json()
        result = srv.bot.word_store.update(
            str(body.get('scope', '')),
            body.get('use_keyword'),
            body.get('use_blocked'))
        return json_response({'ok': True, 'scopes': result})
    except Exception as exc:
        return json_response({'ok': False, 'error': str(exc)}, status=400)


ROUTES = [
    web.get('/api/words', handle_words_get),
    web.post('/api/words', handle_words_post),
    web.get('/api/words/scopes', handle_word_scopes_get),
    web.post('/api/words/scopes', handle_word_scopes_post),
]
