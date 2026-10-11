"""Explicit compatibility exports for URL views during modular refactoring.

Keep this module importable as ``from . import views`` for ``bot_app.urls`` while
view implementations live in focused modules under ``bot_app.views``.  Avoid a
legacy wildcard import here so URL-bound names fail loudly if an explicit export
is missing.
"""

from .common import _normalize_inn, _normalize_phone_digits
from .bot_api import (
    api_receive_sale,
    api_receive_application,
    api_receive_verification,
    _check_bearer_token,
    _find_client_id,
    _source_label,
    _notify,
)
from .auth import (
    login_page,
    logout_view,
    hash_password,
    verify_password,
    _ensure_login_attempts_table,
    _get_client_ip,
    _check_login_rate_limit,
    _record_login_attempt,
)
from .calendar import (
    api_calendar_events,
    api_calendar_event_save,
    api_calendar_event_delete,
    api_calendar_categories,
)
from .dashboard import (
    dashboard,
    analytics,
    _get_overall_stats,
    _get_financial_stats,
    _get_top_buttons,
    _get_activity_timeline,
    _get_top_users,
    _get_specialists_stats,
    _get_expiring,
    _get_conversion_funnel,
    _get_events_by_type,
    _get_popular_actions,
    _has_source_platform_column,
    _get_referral_stats,
    _get_users_by_referral,
)
from .companies import (
    api_contacts_search,
    crm_companies_list,
    crm_company_contact_add,
    crm_company_contact_remove,
    crm_company_create,
    crm_company_delete,
    crm_company_detail,
    crm_company_edit,
    crm_company_kkt_add,
    crm_company_kkt_delete,
    crm_company_kkt_update,
)
from .clients import (
    _build_client_display,
    _ensure_client_notes_table,
    api_alfabank_ip_refresh,
    api_client_note_delete,
    api_client_note_save,
    api_client_notes,
    api_clients_search,
    crm_contacts_list,
    crm_client_create,
    crm_client_detail,
    crm_client_edit,
    crm_clients_list,
    crm_custom_field_delete,
    crm_custom_field_save,
    crm_kkt_add,
    crm_kkt_delete,
    crm_kkt_update,
)
from .orders import (
    crm_orders_list,
    crm_order_detail,
    api_orders_stats,
    export_orders_excel,
    api_order_manual_create,
    api_order_update,
    api_order_delete,
    api_order_create_client,
    api_orders_search_clients,
    _ensure_orders_manual_columns,
    _ensure_address_columns,
    _get_order_comment_column,
    _ensure_scp_order_id_column,
)
from .settings import (
    system_settings_page,
    external_products_page,
    _ensure_system_settings_table,
    _get_system_settings_rows,
    _load_external_catalog_seed,
    _ensure_external_catalog_tables,
    _refresh_external_products_catalog,
    _get_external_products_rows,
    _get_shop_products_rows,
)
from .notifications import (
    notifications_list,
    notifications_renewal,
    notifications_calendar,
    api_notification_read,
    api_notification_dismiss,
    api_notifications_read_all,
    api_notifications_count,
    _notifications_page_context,
    _ensure_notification_tables,
    _sync_kkt_expiry_notifications,
    _get_unread_notif_count,
)

from .tasks import (
    TASK_CLIENT_ACCESS_STATUSES,
    crm_tasks_list,
    export_tasks_excel,
    api_tasks_stats,
    api_task_create,
    api_task_update,
    api_task_delete,
    _ensure_tasks_table,
    _ensure_tasks_schema,
    _ensure_task_history_table,
    _add_task_history,
    _sync_deal_and_order_tasks_from_orders,
    _upsert_task,
    _normalize_linked_client_ids,
    _resolve_task_linked_client_ids,
    _fetch_clients_brief,
    _ensure_task_clients_table,
    _set_task_clients,
    _get_task_client_ids,
    _specialist_has_task_client_access,
    _get_default_director_id,
    _build_client_brief,
    _extract_linked_client_ids_from_meta,
    _task_requires_card_verification_before_complete,
)

from .specialists import (
    specialists_manage,
    api_specialist_save,
    _count_specialist_refs,
    _get_fk_dependencies,
    api_specialist_delete,
)
from .specialist import (
    specialist_panel,
)

from .chats import (
    specialist_chat,
    crm_chat_detail,
    crm_chats_list,
    api_chat_create_payment,
    api_send_message,
    api_chat_upload_file,
    api_chat_download_file,
    api_chat_close_by_agent,
    api_crm_chat_force_close,
    api_crm_chat_transfer,
    api_chat_messages,
    api_crm_chats_unread_state,
)
from .partner import (
    _find_or_link_partner_user_id,
    _is_partner_client_accessible,
    _is_partner_company_accessible,
    _ensure_partner_director_chat_for_site,
    _resolve_partner_identity,
    partner_clients_list,
    partner_client_create,
    partner_client_detail,
    partner_chats_list,
    partner_analytics,
    partner_companies_list,
    partner_company_create,
    partner_company_detail,
    partner_company_edit,
    partner_company_delete,
    partner_company_contact_add,
    partner_company_contact_remove,
    partner_company_kkt_add,
    partner_company_kkt_delete,
    partner_company_kkt_update,
    api_partner_contacts_search,
)
