"""转发统计存储。

- 全局表 `daily_stats`：所有任务汇总，按「天」维度累计；
- 分类表 `daily_scope_stats`：按 `scope`（monitor / manual）分别累计，
  这样「实时监控概览」与「手动转发概览」可以各自展示自己的今日数据与趋势。

只暴露白名单字段，避免 SQL 注入。
"""

import os
import sqlite3
from datetime import date, timedelta

# 允许统计的字段白名单（列名 -> 中文名）
FIELDS = {
    'forwarded': '成功转发',
    'failed': '转发失败',
    'skipped': '跳过（屏蔽词/去重/来源即目标）',
    'matched': '命中关键词',
}

SCOPES = ('monitor', 'manual')


class StatsStore:
    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute(
                '''CREATE TABLE IF NOT EXISTS daily_stats (
                    day TEXT PRIMARY KEY,
                    forwarded INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    matched INTEGER NOT NULL DEFAULT 0
                )'''
            )
            conn.execute(
                '''CREATE TABLE IF NOT EXISTS daily_scope_stats (
                    day TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    forwarded INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    matched INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (day, scope)
                )'''
            )

    # ---------------- 写入 ----------------
    def _today(self):
        return date.today().isoformat()

    @staticmethod
    def _scope(scope):
        return scope if scope in SCOPES else None

    def inc(self, field, n=1, scope=None):
        """给今天的某个计数字段累加 n；scope 非空时同时累加到分类统计。"""
        if field not in FIELDS:
            raise ValueError('未知统计字段: %s' % field)
        day = self._today()
        scope = self._scope(scope)
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.execute('INSERT OR IGNORE INTO daily_stats(day) VALUES(?)', (day,))
            # field 已通过白名单校验，可安全拼接
            conn.execute(
                'UPDATE daily_stats SET %s = %s + ? WHERE day = ?' % (field, field),
                (int(n), day),
            )
            if scope:
                conn.execute('INSERT OR IGNORE INTO daily_scope_stats(day, scope) VALUES(?,?)', (day, scope))
                conn.execute(
                    'UPDATE daily_scope_stats SET %s = %s + ? WHERE day = ? AND scope = ?' % (field, field),
                    (int(n), day, scope),
                )

    # ---------------- 读取 ----------------
    def today(self, scope=None):
        return self._row(self._today(), scope)

    def _row(self, day, scope=None):
        scope = self._scope(scope)
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            if scope:
                cur = conn.execute(
                    'SELECT day, forwarded, failed, skipped, matched FROM daily_scope_stats '
                    'WHERE day = ? AND scope = ?', (day, scope))
            else:
                cur = conn.execute(
                    'SELECT day, forwarded, failed, skipped, matched FROM daily_stats WHERE day = ?', (day,))
            r = cur.fetchone()
        out = {'day': day, 'forwarded': 0, 'failed': 0, 'skipped': 0, 'matched': 0}
        if scope:
            out['scope'] = scope
        if r:
            out.update({'forwarded': r[1], 'failed': r[2], 'skipped': r[3], 'matched': r[4]})
        return out

    def totals(self, scope=None):
        scope = self._scope(scope)
        if scope:
            sql = ('SELECT COALESCE(SUM(forwarded),0), COALESCE(SUM(failed),0), '
                   'COALESCE(SUM(skipped),0), COALESCE(SUM(matched),0) FROM daily_scope_stats WHERE scope = ?')
            args = (scope,)
        else:
            sql = ('SELECT COALESCE(SUM(forwarded),0), COALESCE(SUM(failed),0), '
                   'COALESCE(SUM(skipped),0), COALESCE(SUM(matched),0) FROM daily_stats')
            args = ()
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            r = conn.execute(sql, args).fetchone()
        return {'forwarded': r[0], 'failed': r[1], 'skipped': r[2], 'matched': r[3]}

    def recent_days(self, days=7, scope=None):
        """返回最近 days 天（含今天）的统计，缺失的日期补 0。"""
        days = max(1, min(int(days), 60))
        scope = self._scope(scope)
        end = date.today()
        start = end - timedelta(days=days - 1)
        if scope:
            sql = ('SELECT day, forwarded, failed, skipped, matched FROM daily_scope_stats '
                   'WHERE scope = ? AND day >= ? ORDER BY day ASC')
            args = (scope, start.isoformat())
        else:
            sql = ('SELECT day, forwarded, failed, skipped, matched FROM daily_stats '
                   'WHERE day >= ? ORDER BY day ASC')
            args = (start.isoformat(),)
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            rows = {r[0]: r for r in conn.execute(sql, args).fetchall()}
        out = []
        for i in range(days):
            d = (start + timedelta(days=i)).isoformat()
            r = rows.get(d)
            out.append({
                'day': d,
                'label': d[5:],
                'forwarded': r[1] if r else 0,
                'failed': r[2] if r else 0,
                'skipped': r[3] if r else 0,
                'matched': r[4] if r else 0,
            })
        return out
