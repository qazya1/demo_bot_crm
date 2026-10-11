"""Shared access checks used by permissions without importing view modules."""

from django.db import connection

from .services.task_service import (
    TASK_CLIENT_ACCESS_STATUSES,
    _extract_linked_client_ids_from_meta,
    _get_task_client_ids,
    _normalize_linked_client_ids,
    _resolve_task_linked_client_ids,
    _specialist_has_task_client_access as specialist_has_task_client_access,
)
from .sql_cursor import with_sql_rewrite

connection = with_sql_rewrite(connection)


def is_partner_client_accessible(partner_id, client_id):
    """Return whether a client belongs to the given partner specialist."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM users
            WHERE id = %s
              AND (added_by_partner_id = %s OR source_partner_id = %s)
            LIMIT 1
            """,
            [client_id, partner_id, partner_id],
        )
        return cursor.fetchone() is not None
