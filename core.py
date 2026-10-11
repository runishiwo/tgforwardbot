"""公共基础：路径常量、带时间戳的打印、行文本切分等小工具。"""

import json
import os
import re
import sys
from datetime import datetime

from aiohttp import web

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(BASE_DIR, 'ui')


def json_response(data, status=200):
    """统一的 JSON 响应（中文不转义）。"""
    return web.json_response(data, status=status,
                             dumps=lambda o: json.dumps(o, ensure_ascii=False))


def server_of(request):
    """取出挂在 aiohttp app 上的 WebServer 实例。"""
    return request.app['srv']


def now_text():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def print_ts(*args, **kwargs):
    """带时间戳打印并立即 flush，方便看 systemd 日志。"""
    print('[%s]' % now_text(), *args, **kwargs)
    sys.stdout.flush()


def split_lines(value):
    """把前端 textarea 文本切成词条列表；已经是 list 就直接返回。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return [ln for ln in str(value).splitlines()]


# 与前端词表的切分规则保持一致：支持换行或 ;；,，、 分隔
_WORD_SPLIT_RE = re.compile(r'[;；,，、\n\r\t　]+')


def split_words(value):
    """把关键词/屏蔽词文本切成词条列表：支持换行或 ;；,，、 分隔；已是 list 则逐元素再切。

    - 以 `##` 开头的整行视为注释（与词表文件规则一致），直接忽略；
    - 这样任务表单可以直接粘贴词表预览里 `;` 连接的文本，甚至整份词表文件内容。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out = []
        for x in value:
            out.extend(split_words(x))
        return out
    chunks = []
    for line in str(value).splitlines():
        if line.strip().startswith('##'):
            continue
        chunks.extend(_WORD_SPLIT_RE.split(line))
    return [p.strip() for p in chunks if p and p.strip()]


def read_lines(path):
    """读取一个纯文本行表（用于种子词表 / targets.txt）。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except Exception:
        return []
