"""Compatibility exports for company/client schema helpers.

The implementation lives in ``bot_app.services.company_schema`` so service
modules do not depend on the view package.
"""

from ..services.company_schema import (  # noqa: F401
    _auto_create_or_link_company,
    _ensure_added_by_columns,
    _ensure_client_kkt_company_column,
    _ensure_companies_tables,
    _ensure_users_birth_date_column,
)
