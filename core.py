"""公共基础：路径常量、带时间戳的打印、行文本切分等小工具。"""

import json
import os
import re
import sys
from datetime import datetime

from aiohttp import web

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(BASE_DIR, 'ui')

# 任务日志的类别标签：写在时间戳之后（`[时间] [类别] 内容`），供网页端按类别筛选。
# 顺序即网页上筛选按钮的顺序：全部 → 直转 → 下载转 → 屏蔽 → 未命中 → 失败。
# 「直转」  = 直接转发成功（文本直发 / 引用重发，不经过本机磁盘）；
# 「下载转」= 媒体引用被服务器拒绝，降级为「下载到本机再作为新文件上传」后成功。
# 「未命中」= 没有转发出去但也不算错（已转发过/任务内重复/来源即目标）。
TAG_OK_DIRECT = '直转'
TAG_OK_DOWNLOAD = '下载转'
TAG_FAIL = '失败'
TAG_SKIP = '未命中'
TAG_BLOCK = '屏蔽'
LOG_TAGS = (TAG_OK_DIRECT, TAG_OK_DOWNLOAD, TAG_BLOCK, TAG_SKIP, TAG_FAIL)

# 旧版本日志里成功只有笼统的「成功」一个标签（分不清是直转还是下载转），
# 仅用于识别历史日志，新日志不再使用。
TAG_OK_LEGACY = '成功'


def json_response(data, status=200):
    """统一的 JSON 响应（中文不转义）。"""
    return web.json_response(data, status=status,
                             dumps=lambda o: json.dumps(o, ensure_ascii=False))


def server_of(request):
    """取出挂在 aiohttp app 上的 WebServer 实例。"""
    return request.app['srv']


def now_text():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def preview_text(text, limit=50):
    """把消息正文压成一行短预览，用于任务日志。

    - 空白/换行统一折叠成单个空格；
    - 纯媒体（无文字）消息返回占位符；
    - 超长截断并加省略号。
    """
    body = ' '.join(str(text or '').split())
    if not body:
        return '［无文字/媒体］'
    return body if len(body) <= limit else body[:limit] + '…'


def id_range_text(ids):
    """把一组消息 id 压成「消息 1 - 9」/「消息 5」这样的短区间描述。"""
    nums = []
    for x in ids or ():
        try:
            nums.append(int(x))
        except (TypeError, ValueError):
            continue
    if not nums:
        return ''
    lo, hi = min(nums), max(nums)
    return '消息 %d' % lo if lo == hi else '消息 %d - %d' % (lo, hi)


def print_ts(*args, **kwargs):
    """带时间戳打印并立即 flush，方便看 systemd 日志。"""
    print('[%s]' % now_text(), *args, **kwargs)
    sys.stdout.flush()


def err_text(exc):
    """异常的可读描述。

    有些异常（尤其 asyncio.TimeoutError / 部分连接错误）的 str() 是**空字符串**，
    直接 `%s` 打出来日志只剩一个冒号，很难查。这里兜底成「异常类型 + 中文说明」。
    """
    try:
        msg = str(exc).strip()
    except Exception:
        msg = ''
    if msg:
        return msg
    name = type(exc).__name__
    if name in ('TimeoutError', 'CancelledError'):
        return '%s：等待服务器响应超时（网络慢或文件较大）' % name
    return '%s（没有附带描述信息）' % name


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
