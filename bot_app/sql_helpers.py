"""Shared low-level SQL helpers for CRM views."""

from django.db import connection

from .sql_cursor import with_sql_rewrite

connection = with_sql_rewrite(connection)


def _get_table_id_column_type(cursor, table_name, fallback='BIGINT'):
    """Возвращает COLUMN_TYPE для первичного id таблицы (например, `int(11) unsigned`)."""
    try:
        cursor.execute(f"SHOW COLUMNS FROM `{table_name}` LIKE 'id'")
        row = cursor.fetchone()
        if row and row[1]:
            return row[1]
    except Exception:
        pass
    return fallback


def _column_exists(table_name, column_name):
    """Проверить наличие колонки в текущей БД MySQL/MariaDB."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = %s
              AND column_name = %s
            """,
            [table_name, column_name],
        )
        return cursor.fetchone()[0] > 0


def _table_and_column_exist(cursor, table_name: str, column_name: str) -> bool:
    """Проверка существования таблицы и колонки в текущей БД."""
    cursor.execute(
        """
        SELECT COUNT(*)
          FROM INFORMATION_SCHEMA.COLUMNS
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME = %s
           AND COLUMN_NAME = %s
        """,
        [table_name, column_name],
    )
    row = cursor.fetchone()
    return bool(row and int(row[0] or 0) > 0)


def _dictfetchall(cursor):
    """Return all rows from a cursor as dictionaries."""
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _dictfetchone(cursor):
    """Return one row from a cursor as a dictionary."""
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [col[0] for col in cursor.description]
    return dict(zip(columns, row))
