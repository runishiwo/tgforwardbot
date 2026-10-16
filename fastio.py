# -*- coding: utf-8 -*-
"""并发分片上传 / 下载。

Telethon 原生的 ``upload_file`` / ``download_file`` 都是**串行分片**：
一片 RPC 发出去、等响应回来，才发下一片。延迟高的链路下吞吐被 RTT 卡死
（128KB/片 ÷ RTT，200ms RTT 时也只有 ~0.6MB/s），带宽大量闲置。

这里改成 N 个分片并发（协议本身支持：同一 ``file_id`` 下不同
``part_index`` 可并发 ``saveBigFilePart``；下载侧 ``getFile`` 可按
``offset`` 并发取任意分片），并把「某一片失败」的重试粒度从整个文件
缩小到单个分片。

任一步遇到协议不支持并发的情况（CDN 重定向等）会抛 ``Fallback``，
由上层回退到 Telethon 原生串行实现，保证正确性优先。
"""
import asyncio
import hashlib
import logging
import os

from telethon import helpers, utils
from telethon.tl import custom, functions, types

log = logging.getLogger('fastio')

DEFAULT_CONCURRENCY = 16      # 并发分片数
PART_RETRIES = 4              # 单个分片失败重试次数
SMALL_FILE_LIMIT = 10 * 1024 * 1024   # >10MB 走 saveBigFilePart（与 Telethon 一致）
MAX_PART_SIZE = 512 * 1024    # 协议允许的单片上限（Telethon 默认只用 128KB，太保守）


class Fallback(Exception):
    """需要回退到 Telethon 原生实现的信号（如遇到 CDN 重定向）。"""


async def fast_upload(client, path, concurrency=DEFAULT_CONCURRENCY):
    """并发上传本地文件，返回可直接交给 ``send_file`` 的 InputFile 对象。

    等价于 ``await client.upload_file(path)``，但分片并发。
    """
    size = os.path.getsize(path)
    # 用协议允许的最大分片（512KB）：Telethon 默认只按文件大小选 128KB，
    # 高延迟链路下一半以上时间耗在等 RTT 上；512KB/片把 RTT 占比压到 1/4。
    part_size = MAX_PART_SIZE
    is_big = size > SMALL_FILE_LIMIT
    part_count = (size + part_size - 1) // part_size
    file_id = helpers.generate_random_long()
    file_name = os.path.basename(path)
    sem = asyncio.Semaphore(max(1, concurrency))

    def _read(index):
        with open(path, 'rb') as f:
            f.seek(index * part_size)
            return f.read(part_size)

    async def _send(index):
        part = _read(index)
        req = (functions.upload.SaveBigFilePartRequest(file_id, index, part_count, part)
               if is_big else
               functions.upload.SaveFilePartRequest(file_id, index, part))
        last = None
        for attempt in range(PART_RETRIES):
            try:
                async with sem:
                    ok = await client(req)
                if ok:
                    return
                last = RuntimeError('saveBigFilePart returned false (part %d)' % index)
            except Exception as e:      # 网络抖动：只重试这一片
                last = e
            await asyncio.sleep(min(0.4 * (attempt + 1), 2.0))
        raise last

    await asyncio.gather(*[_send(i) for i in range(part_count)])
    if is_big:
        return types.InputFileBig(file_id, part_count, file_name)
    md5 = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            md5.update(chunk)
    return custom.InputSizedFile(file_id, part_count, file_name, md5=md5, size=size)


def _doc_name(message, default='file'):
    doc = getattr(getattr(message, 'media', None), 'document', None)
    for a in (getattr(doc, 'attributes', None) or []):
        if isinstance(a, types.DocumentAttributeFilename) and a.file_name:
            return a.file_name
    return '%s_%s' % (default, getattr(message, 'id', '0'))


async def fast_download(client, message, dest_dir, concurrency=DEFAULT_CONCURRENCY):
    """并发下载消息里的媒体到 ``dest_dir``，返回本地文件路径。

    等价于 ``await client.download_media(message, file=dest_dir)``，但分片并发。
    """
    try:
        info = utils._get_file_info(message)
        location, size = info.location, info.size or 0
    except Exception:
        doc = getattr(message, 'document', None)
        if doc is None:
            doc = getattr(getattr(message, 'media', None), 'document', None)
        if doc is None:
            raise Fallback('无法解析媒体位置')
        info = utils._get_file_info(doc)
        location, size = info.location, info.size or 0
    if not os.path.isdir(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, _doc_name(message))
    part_size = MAX_PART_SIZE
    part_count = (size + part_size - 1) // part_size
    sem = asyncio.Semaphore(max(1, concurrency))

    # 预分配文件，各分片按 offset 直接落位
    with open(path, 'wb') as f:
        f.truncate(size)

    async def _fetch(index):
        offset = index * part_size
        # limit 固定为 512KB（4096 对齐）：Telegram 要求 limit 必须是 4096 的倍数，
        # 对最后一片会直接返回不足一个 limit 的数据（Telethon 原生也是这个套路）。
        limit = part_size
        last = None
        for attempt in range(PART_RETRIES):
            try:
                async with sem:
                    data = await client(functions.upload.GetFileRequest(
                        location, offset, limit))
            except Exception as e:      # 网络抖动：只重试这一片
                last = e
                await asyncio.sleep(min(0.4 * (attempt + 1), 2.0))
                continue
            if isinstance(data, types.upload.FileCdnRedirect):
                raise Fallback('CDN 文件，改用原生下载')
            if not isinstance(data, types.upload.File) or not data.bytes:
                last = RuntimeError('getFile 返回空 (offset %d)' % offset)
                await asyncio.sleep(min(0.4 * (attempt + 1), 2.0))
                continue
            with open(path, 'r+b') as f:
                f.seek(offset)
                f.write(data.bytes)
            return
        raise last

    try:
        await asyncio.gather(*[_fetch(i) for i in range(part_count)])
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path
