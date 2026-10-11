"""TG 转发机器人 —— 入口与总览。

程序结构（各模块职责一览）：

    core.py      公共基础：路径常量、带时间戳打印、JSON 响应、文本切行
    words.py     词表：实时监控 / 手动转发**各用各的**关键词表与屏蔽词表 + 两个开关
    tasks.py     任务中心底座：任务表(data/tasks.db)、每条任务的独立日志(log/tasks/)、
                 转发锚点(data/anchors.db)、任务中心接口
    stats.py     统计：全局 + 按类别（实时 / 手动）的按天计数
    monitor.py   【实时监控】多条任务并行运行，各自来源/目的/词表/开关/日志 + 接口
    manual.py    【手动转发】按频道与时间范围补转历史消息，可暂停/恢复/编辑 + 接口
    bot.py       转发核心：Telegram 客户端、登录、消息投递、事件监听、断线补扫、日志清理
    web.py       Web 管理台：鉴权、登录页、概览/日志/频道接口，并汇总各模块路由
    main.py      本文件：读配置 -> 组装 -> 启动 / 退出

启动：python3 main.py（线上由 systemd 服务 tgforwardbot 托管）
"""

import asyncio
import logging
import os
import signal
import sys

import yaml

from bot import ForwardBot
from core import print_ts
from web import WebServer


async def main():
    # 设置 aiohttp.access 日志等级为 WARNING，避免访问日志干扰
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
    logging.getLogger('aiohttp.access').propagate = False

    if not os.path.exists('config.yml'):
        print_ts('找不到配置文件 config.yml，请先创建。示例内容：\n'
                 'api_id: 123456\n'
                 'api_hash: "your_api_hash"\n'
                 'target_entity: -1001234567890\n'
                 'keyword_file: "keyword.txt"\n'
                 'session_name: "session_session"\n'
                 'reload_interval: 60\n'
                 'forward_interval: 5\n'
                 'max_retries: 3\n'
                 'log_dir: "log"\n'
                 'log_cleanup_interval: 21600\n'
                 'album_scan_limit: 50\n'
                 'media_tmp_dir: "media_tmp"\n'
                 'web_host: "0.0.0.0"\n'
                 'web_port: 8080\n'
                 'web_password: "123456"\n')
        return

    with open('config.yml', 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    bot = ForwardBot(config)
    web_server = WebServer(bot, host=config.get('web_host', '0.0.0.0'),
                           port=config.get('web_port', 8080),
                           password=config.get('web_password', '123456'))

    await bot.start()
    await web_server.start()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def ask_exit():
        print_ts('收到退出信号，准备关闭程序...')
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, ask_exit)

    await stop_event.wait()
    await web_server.stop()
    await bot.stop()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
