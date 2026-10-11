"""
Локальный прокси для Django connection/cursor с переписыванием SQL на *_test таблицы.
Используется только в bot_app raw-SQL, чтобы не править сотни вызовов вручную.
"""
from __future__ import annotations

from db.connection import _rewrite_sql_for_test_contour


class RewritingCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        rewritten = _rewrite_sql_for_test_contour(sql)
        return self._cursor.execute(rewritten, params)

    def executemany(self, sql, param_list):
        rewritten = _rewrite_sql_for_test_contour(sql)
        return self._cursor.executemany(rewritten, param_list)

    def __enter__(self):
        entered = self._cursor.__enter__()
        return RewritingCursor(entered)

    def __exit__(self, exc_type, exc, tb):
        return self._cursor.__exit__(exc_type, exc, tb)

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, item):
        return getattr(self._cursor, item)


class RewritingConnection:
    def __init__(self, connection):
        self._connection = connection

    def cursor(self, *args, **kwargs):
        return RewritingCursor(self._connection.cursor(*args, **kwargs))

    def __getattr__(self, item):
        return getattr(self._connection, item)


def with_sql_rewrite(connection):
    return RewritingConnection(connection)

