"""Web 管理台：鉴权、登录页、概览/日志/频道接口，并汇总各模块的 API 路由。

各领域自己的接口挂在各自的模块里（`words.py` / `tasks.py` / `monitor.py` / `manual.py`），
本模块只做「组装 + 公共接口」。
"""

import asyncio
import base64
import hashlib
import html as html_mod
import logging
import os
import re
from datetime import datetime
from string import Template

from aiohttp import web

from core import UI_DIR, json_response, print_ts, server_of
from manual import ROUTES as MANUAL_ROUTES
from manual import ManualForward
from monitor import ROUTES as MONITOR_ROUTES
from tasks import (MONITOR, ROUTES as TASK_ROUTES, list_public)
from words import ROUTES as WORD_ROUTES


# 静态资源 / 拼装页面 的内存缓存：以 (路径, mtime, size) 为失效键，
# 避免每次请求都重读文件；内容变化时（mtime 变）自动重读并重算 ETag。
_STATIC_CACHE = {}
_ADMIN_CACHE = {}

# 字体文件（MiSans）扁平放在 ui/fonts/ 下：只允许「字母数字开头的文件名」，杜绝路径穿越。
_FONT_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
_FONT_TYPES = {'.css': 'text/css', '.woff2': 'font/woff2', '.woff': 'font/woff',
               '.ttf': 'font/ttf', '.otf': 'font/otf'}

# 管理台由这些片段按序拼装（运行时由 web.py 读取后拼接）
_ADMIN_PARTS = (
    'head.html',
    'view-dashboard.html',
    'view-dashboard-monitor.html',
    'view-dashboard-manual.html',
    'view-tasks.html',
    'view-taskdetail.html',
    'view-monitor.html',
    'view-manual.html',
    'view-words.html',
    'tail.html',
)


def _json(data, status=200):
    return json_response(data, status)


def _read_cached(path):
    """返回 (content_bytes, etag)；用 mtime/size 做内存缓存，减少磁盘读取。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    cached = _STATIC_CACHE.get(path)
    if cached and cached[0] == (st.st_mtime, st.st_size):
        return cached[1], cached[2]
    with open(path, 'rb') as f:
        content = f.read()
    etag = '"%s"' % hashlib.md5(content).hexdigest()
    _STATIC_CACHE[path] = ((st.st_mtime, st.st_size), content, etag)
    return content, etag


class WebServer:
    def __init__(self, bot, host='0.0.0.0', port=8080, password='123456'):
        self.bot = bot
        self.host = host
        self.port = port
        self.password = password
        self.manual = ManualForward(bot)
        self.app = web.Application(middlewares=[self.auth_middleware])
        self.app['srv'] = self
        routes = [
            web.get('/', self.handle_main),
            web.get('/login', self.handle_login),
            web.post('/login', self.handle_login_post),
            web.get('/logout', self.handle_logout),
            web.get('/admin.css', self.handle_static_css),
            web.get('/admin.js', self.handle_static_js),
            web.get('/fonts/{name}', self.handle_static_font),
            web.get('/telegram-login', self.handle_telegram_login),
            web.post('/api/telegram-login', self.handle_telegram_login_post),
            web.get('/api/status', self.handle_status),
            web.get('/api/stats', self.handle_stats),
            web.get('/api/logs', self.handle_logs),
            web.get('/api/logs/files', self.handle_log_list),
            web.post('/api/logs/cleanup', self.handle_log_cleanup),
            web.get('/api/dialogs', self.handle_dialogs),
            web.post('/api/tasks/state', self.handle_task_state),
        ]
        routes += WORD_ROUTES + TASK_ROUTES + MONITOR_ROUTES + MANUAL_ROUTES
        self.app.add_routes(routes)

    @web.middleware
    async def auth_middleware(self, request, handler):
        if request.path in ('/login', '/logout'):
            return await handler(request)
        expected = base64.b64encode(self.password.encode()).decode()
        auth_header = request.headers.get('Authorization')
        if auth_header and auth_header.startswith('Basic '):
            try:
                _, passwd = base64.b64decode(auth_header[6:]).decode().split(':', 1)
            except Exception:
                passwd = None
            if passwd == self.password:
                return await handler(request)
        if request.cookies.get('fgb_auth') == expected:
            return await handler(request)
        # 浏览器访问跳登录页；API 调用仍走 401 Basic（curl -u 兼容）
        if 'text/html' in request.headers.get('Accept', ''):
            raise web.HTTPFound('/login')
        return web.Response(status=401, headers={
            'WWW-Authenticate': 'Basic realm="Access to ForwardBot"'})

    # ---------------- 页面与登录 ----------------
    def _load_login_page(self, error=''):
        try:
            with open(os.path.join(UI_DIR, 'login.html'), 'r', encoding='utf-8') as f:
                tpl = f.read()
        except Exception as e:
            return web.Response(text='无法加载登录页: %s' % e, status=500,
                                content_type='text/plain; charset=utf-8')
        return web.Response(text=Template(tpl).safe_substitute(error=error),
                            content_type='text/html')

    async def handle_login(self, request):
        if request.cookies.get('fgb_auth') == base64.b64encode(self.password.encode()).decode():
            raise web.HTTPFound('/')
        return self._load_login_page()

    async def handle_login_post(self, request):
        data = await request.post()
        if data.get('password', '') == self.password:
            resp = web.HTTPFound('/')
            resp.set_cookie('fgb_auth', base64.b64encode(self.password.encode()).decode(),
                            max_age=7 * 24 * 3600, httponly=True, path='/')
            raise resp
        return self._load_login_page(error='访问密码错误，请重试')

    async def _serve_admin(self, request):
        # 管理台由 ui/ 下若干 .html 片段在运行时拼装而成；所有数据由前端经 /api/* 拉取，
        # 因此拼好后原样返回，不做 Template 替换（避免与 JS 里的 $ 冲突）。
        base = UI_DIR
        mtimes, paths = [], []
        for part in _ADMIN_PARTS:
            p = os.path.join(base, part)
            try:
                mtimes.append(os.stat(p).st_mtime)
                paths.append(p)
            except OSError:
                return web.Response(text='缺少界面片段: %s' % part, status=500,
                                    content_type='text/plain; charset=utf-8')
        cache_key = (tuple(paths), tuple(mtimes))
        cached = _ADMIN_CACHE.get('admin')
        if cached and cached[0] == cache_key:
            html_bytes, etag = cached[1], cached[2]
        else:
            try:
                chunks = []
                for p in paths:
                    with open(p, 'r', encoding='utf-8') as f:
                        chunks.append(f.read())
                html_bytes = ''.join(chunks).encode('utf-8')
            except Exception as exc:
                return web.Response(text='拼装界面失败: %s' % exc, status=500,
                                    content_type='text/plain; charset=utf-8')
            etag = '"%s"' % hashlib.md5(html_bytes).hexdigest()
            _ADMIN_CACHE['admin'] = (cache_key, html_bytes, etag)
        if request.headers.get('If-None-Match') == etag:
            return web.Response(status=304,
                                headers={'ETag': etag, 'Cache-Control': 'no-cache'})
        return web.Response(body=html_bytes, content_type='text/html',
                            headers={'ETag': etag, 'Cache-Control': 'no-cache'})

    async def handle_main(self, request):
        return await self._serve_admin(request)

    # ---------------- 静态 UI 资源 ----------------
    async def _serve_static(self, request, name):
        """admin.css / admin.js。

        用 `no-cache` 而不是 `max-age=N`：可以存，但每次使用前必须回源校验。
        我们已经算好 ETag，所以未改动时只是一个 304 空响应，改了立刻全量下发 ——
        既不浪费流量，也不会出现「改了样式但浏览器还拿着旧文件」。
        （字体那边文件名带内容 hash，才适合强缓存，见 handle_static_font。）
        """
        path = os.path.join(UI_DIR, name)
        loaded = _read_cached(path)
        if loaded is None:
            return web.Response(text='Not found', status=404)
        content, etag = loaded
        headers = {'ETag': etag, 'Cache-Control': 'no-cache'}
        if request.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers=headers)
        ctype = 'text/css' if name.endswith('.css') else 'application/javascript'
        return web.Response(body=content, content_type=ctype, headers=headers)

    async def handle_static_css(self, request):
        return await self._serve_static(request, 'admin.css')

    async def handle_static_js(self, request):
        return await self._serve_static(request, 'admin.js')

    async def handle_static_font(self, request):
        """ui/fonts/ 下的字体资源（MiSans 的 css + woff2 分片）。

        woff2 文件名带内容 hash，可长期强缓存；css 是入口，缓存 1 天。
        """
        name = request.match_info.get('name', '')
        if not _FONT_NAME_RE.match(name):
            return web.Response(text='Not found', status=404)
        ext = os.path.splitext(name)[1].lower()
        ctype = _FONT_TYPES.get(ext)
        if ctype is None:
            return web.Response(text='Not found', status=404)
        loaded = _read_cached(os.path.join(UI_DIR, 'fonts', name))
        if loaded is None:
            return web.Response(text='Not found', status=404)
        content, etag = loaded
        cache = 'public, max-age=86400' if ext == '.css' else 'public, max-age=31536000, immutable'
        headers = {'ETag': etag, 'Cache-Control': cache}
        if request.headers.get('If-None-Match') == etag:
            return web.Response(status=304, headers=headers)
        return web.Response(body=content, content_type=ctype, headers=headers)

    async def handle_logout(self, request):
        resp = web.HTTPFound('/login')
        resp.del_cookie('fgb_auth', path='/')
        raise resp

    async def handle_telegram_login(self, request):
        status = await self.bot.telegram_auth_status()
        error = request.query.get('error', '')
        return web.Response(text=self._render_telegram_login(status, error),
                            content_type='text/html')

    def _tg_login_css(self):
        """Telegram 登录页样式。与管理台共用「深靛夜空」配色（同一套令牌）。"""
        return """
        :root{
          --bg:oklch(.162 .034 277); --panel:oklch(.219 .030 278); --panel2:oklch(.263 .021 277);
          --border:oklch(.288 .026 275); --border2:oklch(.368 .030 277);
          --text:oklch(.955 .008 271); --muted:oklch(.749 .026 273); --faint:oklch(.603 .032 275);
          --accent:oklch(.791 .134 220); --ok:oklch(.867 .134 165); --err:oklch(.704 .160 15);
          --sunken:oklch(.144 .039 275); --glow-a:oklch(.599 .230 286);
          --ease:cubic-bezier(.22,1,.36,1);
        }
        *{box-sizing:border-box}
        html,body{height:100%}
        body{margin:0;display:flex;align-items:center;justify-content:center;
          background:
            radial-gradient(1000px 620px at 92% -10%,color-mix(in oklab,var(--ok) 11%,transparent),transparent 60%),
            radial-gradient(880px 640px at 4% 108%,color-mix(in oklab,var(--glow-a) 20%,transparent),transparent 62%),
            radial-gradient(760px 520px at 46% 46%,color-mix(in oklab,var(--accent) 6%,transparent),transparent 72%),
            var(--bg);
          background-attachment:fixed;
          font-family:"MiSans-VF",-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
          color:var(--text);-webkit-font-smoothing:antialiased}
        .card{width:100%;max-width:400px;margin:16px;padding:40px 36px 30px;
          background:var(--panel);border:1px solid var(--border);border-radius:20px;
          box-shadow:0 30px 70px -25px oklch(0 0 0 / .85), inset 0 1px 0 0 oklch(1 0 0 / .04);
          animation:rise .42s var(--ease)}
        @keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
        .logo{width:54px;height:54px;margin:0 auto 18px;border-radius:16px;
          background:var(--accent);color:var(--bg);
          display:flex;align-items:center;justify-content:center}
        .logo svg{width:26px;height:26px}
        h1{margin:0 0 6px;font-size:21px;text-align:center;letter-spacing:-.012em}
        .sub{margin:0 0 24px;text-align:center;color:var(--faint);font-size:13px}
        label{display:block;margin-bottom:7px;font-size:12.5px;color:var(--muted)}
        input[type=text],input[type=tel],input[type=password]{width:100%;padding:12px 14px;font-size:16px;
          background:var(--sunken);color:var(--text);border:1px solid var(--border2);
          border-radius:11px;outline:none;font-family:inherit;margin-bottom:16px;
          transition:border-color .18s var(--ease),box-shadow .18s var(--ease)}
        input:focus{border-color:var(--accent);
          box-shadow:0 0 0 3px color-mix(in oklab,var(--accent) 16%,transparent)}
        .btn{display:block;width:100%;padding:13px;font-size:15px;font-weight:600;letter-spacing:1px;
          background:var(--accent);color:var(--bg);border:none;
          border-radius:11px;cursor:pointer;text-align:center;text-decoration:none;
          font-family:inherit;transition:background .18s var(--ease),transform .18s var(--ease)}
        .btn:hover{background:color-mix(in oklab,var(--accent) 86%,white)}
        .btn:active{transform:translateY(1px)}
        .btn.ghost{background:transparent;border:1px solid var(--border2);color:var(--muted);font-weight:600}
        .btn.ghost:hover{background:var(--panel2);color:var(--text);border-color:var(--accent)}
        .err{padding:10px 13px;margin:0 0 16px;border-radius:10px;
          background:color-mix(in oklab,var(--err) 14%,transparent);
          border:1px solid color-mix(in oklab,var(--err) 42%,transparent);
          color:color-mix(in oklab,var(--err) 55%,white);font-size:13px;text-align:center}
        .ok{margin:0 0 18px;padding:12px 14px;border-radius:11px;text-align:center;
          background:color-mix(in oklab,var(--ok) 14%,transparent);
          border:1px solid color-mix(in oklab,var(--ok) 42%,transparent);
          color:var(--ok);font-size:14px}
        .tip{color:var(--muted);font-size:13.5px;line-height:1.7;margin:0 0 18px}
        .tip code{background:var(--sunken);padding:2px 6px;border-radius:6px;color:var(--accent);font-size:12.5px}
        .tip b{color:var(--text)}
        .foot{display:block;text-align:center;margin-top:20px;color:var(--faint);font-size:12px;text-decoration:none}
        .foot:hover{color:var(--accent)}
        .row{display:flex;gap:10px;margin-top:6px}
        .row .btn.ghost{flex:1}
        """

    def _render_telegram_login(self, status, error='', step=None):
        if step is None:
            step = 'code' if status.get('waiting_code') else 'phone'
        error_html = '<div class="err">%s</div>' % html_mod.escape(error) if error else ''
        phone = html_mod.escape(status.get('phone', ''))
        if status.get('authorized'):
            body = ('<div class="ok">当前 Telegram 账号已成功登录</div>'
                    '<a class="btn" href="/">返回管理台</a>')
        elif step == 'code':
            body = ('<p class="tip">验证码已发送至 <b>' + phone + '</b>。'
                    '若账号开启了二步验证，验证通过后还会要求输入二步密码。</p>'
                    '<form method="post" action="/api/telegram-login">'
                    '<input type="hidden" name="action" value="code">'
                    '<label>验证码</label>'
                    '<input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" placeholder="6 位验证码" required>'
                    '<button class="btn" type="submit">验证并登录</button>'
                    '</form>'
                    '<div class="row"><a class="btn ghost" href="/telegram-login">重新输入手机号</a>'
                    '<a class="btn ghost" href="/">返回管理台</a></div>')
        elif step == 'password':
            body = ('<p class="tip">该账号开启了 <b>二步验证</b>，请输入 Telegram 的二步验证密码。</p>'
                    '<form method="post" action="/api/telegram-login">'
                    '<input type="hidden" name="action" value="password">'
                    '<label>二步验证密码</label>'
                    '<input type="password" name="password" autocomplete="current-password" required>'
                    '<button class="btn" type="submit">完成登录</button>'
                    '</form>'
                    '<div class="row"><a class="btn ghost" href="/telegram-login">重新输入手机号</a>'
                    '<a class="btn ghost" href="/">返回管理台</a></div>')
        else:  # phone
            body = ('<p class="tip">输入 Telegram 手机号（需含国家区号），例如 '
                    '<code>+8613800000000</code>，我们将向该号码发送验证码。</p>'
                    '<form method="post" action="/api/telegram-login">'
                    '<input type="hidden" name="action" value="phone">'
                    '<label>手机号</label>'
                    '<input type="tel" name="phone" inputmode="tel" autocomplete="tel" placeholder="+86..." required>'
                    '<button class="btn" type="submit">发送验证码</button>'
                    '</form>'
                    '<a class="foot" href="/">返回管理台</a>')
        return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>连接 Telegram 账号 · ForwardBot</title><style>' + self._tg_login_css() +
                '</style></head><body><div class="card">'
                '<div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                'stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/>'
                '<path d="M22 2 15 22l-4-9-9-4Z"/></svg></div>'
                '<h1>连接 Telegram 账号</h1>'
                '<p class="sub">让转发机器人登录你的 Telegram</p>'
                + error_html + '<div class="body">' + body + '</div></div></body></html>')

    async def handle_telegram_login_post(self, request):
        data = await request.post()
        async with self.bot.auth_lock:
            try:
                from telethon.errors import (PasswordHashInvalidError, PhoneCodeExpiredError,
                                             PhoneCodeInvalidError, SessionPasswordNeededError)
                action = data.get('action', '')
                if action == 'phone':
                    phone = data.get('phone', '').strip()
                    if not phone:
                        raise ValueError('手机号不能为空')
                    await self.bot.prepare_login()
                    last_error = None
                    for attempt in range(3):
                        try:
                            if not self.bot.client.is_connected():
                                await self.bot.client.connect()
                            sent = await self.bot.client.send_code_request(phone)
                            last_error = None
                            break
                        except Exception as exc:
                            last_error = exc
                            try:
                                await self.bot.client.disconnect()
                            except Exception:
                                pass
                            if attempt < 2:
                                await asyncio.sleep(1.5 * (attempt + 1))
                    if last_error is not None:
                        raise RuntimeError('发送验证码失败，请稍后重试：Telegram 连接被中断') from last_error
                    self.bot.auth_phone = phone
                    self.bot.auth_phone_code_hash = sent.phone_code_hash
                    raise web.HTTPFound('/telegram-login')
                if action == 'code':
                    code = data.get('code', '').strip()
                    if not code or not self.bot.auth_phone_code_hash:
                        raise ValueError('验证码状态已失效，请重新发送验证码')
                    try:
                        await self.bot.client.sign_in(phone=self.bot.auth_phone, code=code,
                                                      phone_code_hash=self.bot.auth_phone_code_hash)
                    except SessionPasswordNeededError:
                        return web.Response(text=self._render_telegram_login(
                            await self.bot.telegram_auth_status(), '需要输入二步验证密码', step='password'),
                            content_type='text/html')
                    self.bot.auth_phone = None
                    self.bot.auth_phone_code_hash = None
                    await self.bot.finish_web_login()
                    self._kick_auto_resume()   # 刚登录成功：把待恢复的手动任务拉起来
                    raise web.HTTPFound('/telegram-login')
                if action == 'password':
                    password = data.get('password', '')
                    if not password:
                        raise ValueError('二步验证密码不能为空')
                    await self.bot.client.sign_in(password=password)
                    self.bot.auth_phone = None
                    self.bot.auth_phone_code_hash = None
                    await self.bot.finish_web_login()
                    self._kick_auto_resume()   # 刚登录成功：把待恢复的手动任务拉起来
                    raise web.HTTPFound('/telegram-login')
                raise ValueError('无效操作')
            except web.HTTPException:
                raise
            except (PhoneCodeInvalidError, PhoneCodeExpiredError, PasswordHashInvalidError) as e:
                return web.Response(text=self._render_telegram_login(
                    await self.bot.telegram_auth_status(), str(e)),
                    content_type='text/html', status=400)
            except Exception as e:
                return web.Response(text=self._render_telegram_login(
                    await self.bot.telegram_auth_status(), str(e)),
                    content_type='text/html', status=400)

    # ---------------- 概览 / 日志 ----------------
    async def handle_status(self, request):
        return json_response(self.bot.connection_status())

    async def handle_stats(self, request):
        bot = self.bot
        scoped = {}
        for kind in (MONITOR, 'manual'):
            scope = kind
            scoped[scope] = {
                'today': bot.stats.today(scope),
                'totals': bot.stats.totals(scope),
                'days': bot.stats.recent_days(7, scope),
                'counts': bot.kind_counts(kind),
                'sum': bot.kind_stats(kind),
            }
        counts = bot.task_counts()
        counts['tasks_running'] = counts.get('monitor_running', 0) + counts.get('manual_running', 0)
        counts['tasks_paused'] = counts.get('monitor_paused', 0) + counts.get('manual_paused', 0)
        return json_response({
            'today': bot.stats.today(),
            'totals': bot.stats.totals(),
            'days': bot.stats.recent_days(7),
            'scopes': scoped,
            'queue': bot.queue_size(),
            'uptime_seconds': int((datetime.now() - bot.started_at).total_seconds()),
            'started_at': bot.started_at.strftime('%Y-%m-%d %H:%M:%S'),
            'last_forward_at': bot.last_forward_at,
            'connection': bot.connection_status(),
            'counts': counts,
            'telegram_authorized': bool(getattr(bot, 'telegram_authorized', False)),
            'anchors': bot.anchors.summary(),
            'monitor_tasks': list_public(bot.task_store.all(MONITOR))[:8],
            'recent_tasks': list_public(bot.task_store.all())[:8],
        })

    def _log_files(self):
        try:
            return sorted([f for f in os.listdir(self.bot.log_dir)
                           if f.startswith('forward.log')], reverse=True)
        except Exception:
            return []

    async def handle_log_list(self, request):
        return json_response({'files': self._log_files()})

    async def handle_logs(self, request):
        try:
            tail = int(request.query.get('tail', '800'))
        except (TypeError, ValueError):
            tail = 800
        tail = max(20, min(tail, 5000))
        files = self._log_files()
        name = request.query.get('file', 'forward.log')
        if name not in files:
            name = files[0] if files else 'forward.log'
        path = os.path.join(self.bot.log_dir, name)
        max_bytes = 2 * 1024 * 1024
        mtime = size = 0
        try:
            size = os.path.getsize(path)
            mtime = int(os.path.getmtime(path))
            with open(path, 'rb') as f:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                    text = f.read().decode('utf-8', 'ignore').split('\n', 1)[-1]
                else:
                    text = f.read().decode('utf-8', 'ignore')
            all_lines = text.splitlines()
            truncated = len(all_lines) > tail
            lines = all_lines[-tail:] if truncated else all_lines
        except Exception as exc:
            return json_response({'file': name, 'files': files, 'lines': [], 'mtime': mtime,
                                  'size': size, 'truncated': False, 'error': str(exc)})
        return json_response({'file': name, 'files': files, 'lines': lines, 'mtime': mtime,
                              'size': size, 'truncated': truncated})

    async def handle_log_cleanup(self, request):
        try:
            dropped = await self.bot.cleanup_logs()
            return json_response({'ok': True, 'dropped': dropped})
        except Exception as exc:
            return json_response({'ok': False, 'error': str(exc)}, status=400)

    # ---------------- 频道列表 ----------------
    async def handle_dialogs(self, request):
        postable_only = request.query.get('postable') == '1'
        last_error = None
        for attempt in range(3):
            try:
                if not self.bot.client.is_connected():
                    await self.bot.client.connect()
                items = []
                async for d in self.bot.client.iter_dialogs():
                    chat = d.entity
                    if chat is None or not (d.is_channel and getattr(chat, 'broadcast', False)):
                        continue
                    if postable_only:
                        perms = getattr(chat, 'admin_rights', None)
                        can_post = getattr(perms, 'post_messages', False) if perms else False
                        creator = getattr(chat, 'creator', False)
                        if not (creator or can_post):
                            continue
                    items.append({
                        'id': self.bot.peer_id(chat),
                        'title': d.name or str(getattr(chat, 'id', '')),
                        'username': getattr(chat, 'username', '') or '',
                    })
                items.sort(key=lambda x: x['title'])
                return json_response({'items': items})
            except (ConnectionError, OSError) as exc:
                last_error = exc
                try:
                    await self.bot.client.disconnect()
                except Exception:
                    pass
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
            except Exception as exc:
                logging.error('读取频道列表失败: %s', exc)
                return json_response({'error': '读取频道列表失败，请稍后重试'}, status=503)
        logging.warning('读取频道列表时 Telegram 连接不可用: %s', last_error)
        return json_response({'error': 'Telegram 当前断线，频道列表暂时不可用，请稍后重试'}, status=503)

    # ---------------- 任务启停（实时 / 手动统一入口） ----------------
    async def handle_task_state(self, request):
        srv = server_of(request)
        try:
            body = await request.json()
            task_id = str(body.get('id', ''))
            action = str(body.get('action', ''))
            task = srv.bot.task_store.get(task_id)
            if not task:
                return _json({'ok': False, 'error': '任务不存在'}, status=404)
            if task['kind'] == MONITOR:
                result = await srv.bot.monitors.set_state(task_id, action)
            elif action in ('pause', 'stop'):
                result = await srv.manual.pause(task_id)
            elif action == 'resume':
                result = await srv.manual.resume(task_id)
            else:
                result = {'ok': False, 'error': 'action 必须是 pause 或 resume'}
            return _json(result, status=200 if result.get('ok') else 400)
        except Exception as exc:
            return _json({'ok': False, 'error': str(exc)}, status=400)

    async def start(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        print_ts('Web管理界面启动，访问 http://%s:%s ，访问密码：%s' % (self.host, self.port, self.password))
        # 后台自动恢复「上次没跑完」的手动转发任务（服务重启 / 断电中断），
        # 避免任务一直停在「已停用」直到用户自己想起来点「恢复」。
        self._kick_auto_resume()

    def _kick_auto_resume(self):
        """后台触发一次手动任务自动恢复；不阻塞启动，也不影响登录响应。"""
        try:
            asyncio.create_task(self.manual.auto_resume_all())
        except RuntimeError as exc:          # 没有运行中的事件循环（理论上不会发生）
            print_ts('启动手动任务自动恢复失败：%s' % exc)

    async def stop(self):
        # 先把正在跑的手动转发任务停下来（记为「已停用」，可恢复），
        # 再关 Web 服务；此时 Telegram 客户端还没断，收尾写日志是安全的。
        try:
            await self.manual.stop_all()
        except Exception as exc:
            print_ts('停止手动转发任务时出错：%s' % exc)
        await self.app.shutdown()
        await self.app.cleanup()
