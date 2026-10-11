from django.urls import path
from . import views
from . import views_loyalty

app_name = 'bot_app'

urlpatterns = [
    # Авторизация
    path('login/', views.login_page, name='login'),
    path('logout/', views.logout_view, name='logout'),

    # Панель специалиста
    path('specialist/', views.specialist_panel, name='specialist_panel'),
    path('specialist/chat/<int:chat_id>/', views.specialist_chat, name='specialist_chat'),
    path('api/chat/<int:chat_id>/payment/', views.api_chat_create_payment, name='api_chat_create_payment'),
    path('api/chat/<int:chat_id>/send/', views.api_send_message, name='api_send_message'),
    path('api/chat/<int:chat_id>/upload/', views.api_chat_upload_file, name='api_chat_upload_file'),
    path('api/chat/file/<int:file_id>/download/', views.api_chat_download_file, name='api_chat_download_file'),

    # Управление специалистами (только директор)
    path('specialists/', views.specialists_manage, name='specialists_manage'),
    path('api/specialist/save/', views.api_specialist_save, name='api_specialist_save'),
    path('api/specialist/<int:specialist_id>/delete/', views.api_specialist_delete, name='api_specialist_delete'),

    # Главные страницы (только директор)
    path('', views.dashboard, name='dashboard'),
    path('analytics/', views.analytics, name='analytics'),
    path('settings/', views.system_settings_page, name='system_settings'),
    path('settings/external-products/', views.external_products_page, name='external_products'),
    path('crm/', views.crm_clients_list, name='crm_clients'),
    path('crm/chats/<int:chat_id>/', views.crm_chat_detail, name='crm_chat_detail'),
    path('crm/client/create/', views.crm_client_create, name='crm_client_create'),
    path('crm/client/<int:client_id>/', views.crm_client_detail, name='crm_client_detail'),
    path('partner/clients/', views.partner_clients_list, name='partner_clients'),
    path('partner/clients/create/', views.partner_client_create, name='partner_client_create'),
    path('partner/clients/<int:client_id>/', views.partner_client_detail, name='partner_client_detail'),
    path('partner/companies/', views.partner_companies_list, name='partner_companies'),
    path('partner/companies/create/', views.partner_company_create, name='partner_company_create'),
    path('partner/companies/<int:company_id>/', views.partner_company_detail, name='partner_company_detail'),
    path('partner/companies/<int:company_id>/edit/', views.partner_company_edit, name='partner_company_edit'),
    path('partner/companies/<int:company_id>/delete/', views.partner_company_delete, name='partner_company_delete'),
    path('partner/companies/<int:company_id>/contact/add/', views.partner_company_contact_add, name='partner_company_contact_add'),
    path('partner/companies/<int:company_id>/contact/<int:contact_id>/remove/', views.partner_company_contact_remove, name='partner_company_contact_remove'),
    path('partner/companies/<int:company_id>/kkt/add/', views.partner_company_kkt_add, name='partner_company_kkt_add'),
    path('partner/companies/<int:company_id>/kkt/<int:kkt_id>/delete/', views.partner_company_kkt_delete, name='partner_company_kkt_delete'),
    path('partner/companies/<int:company_id>/kkt/<int:kkt_id>/update/', views.partner_company_kkt_update, name='partner_company_kkt_update'),
    path('partner/chats/', views.partner_chats_list, name='partner_chats'),
    path('partner/chats/<int:chat_id>/', views.specialist_chat, name='partner_chat_detail'),
    path('partner/analytics/', views.partner_analytics, name='partner_analytics'),
    path('crm/chats/', views.crm_chats_list, name='crm_chats'),
    path('api/crm/chats/unread-state/', views.api_crm_chats_unread_state, name='api_crm_chats_unread_state'),
    path('api/chat/<int:chat_id>/close/', views.api_chat_close_by_agent, name='api_chat_close_by_agent'),
    path('api/crm/chat/<int:chat_id>/force-close/', views.api_crm_chat_force_close, name='api_crm_chat_force_close'),
    path('api/crm/chat/<int:chat_id>/transfer/', views.api_crm_chat_transfer, name='api_crm_chat_transfer'),
    path('crm/orders/', views.crm_orders_list, name='crm_orders'),
    path('crm/orders/stats/', views.api_orders_stats, name='api_orders_stats'),
    path('crm/orders/export-excel/', views.export_orders_excel, name='export_orders_excel'),
    path('crm/orders/<int:order_id>/', views.crm_order_detail, name='crm_order_detail'),
    path('crm/orders/create/', views.api_order_manual_create, name='api_order_manual_create'),
    path('crm/orders/search-clients/', views.api_orders_search_clients, name='api_orders_search_clients'),
    path('crm/orders/create-client/', views.api_order_create_client, name='api_order_create_client'),
    path('crm/orders/<int:order_id>/update/', views.api_order_update, name='api_order_update'),
    path('crm/orders/<int:order_id>/delete/', views.api_order_delete, name='api_order_delete'),

    # API для редактирования
    path('crm/client/<int:client_id>/edit/', views.crm_client_edit, name='crm_client_edit'),
    path('crm/client/<int:client_id>/alfabank-ip-refresh/', views.api_alfabank_ip_refresh, name='api_alfabank_ip_refresh'),
    path('crm/client/<int:client_id>/custom-field/', views.crm_custom_field_save, name='crm_custom_field_save'),
    path('crm/client/<int:client_id>/custom-field/<int:field_id>/delete/', views.crm_custom_field_delete, name='crm_custom_field_delete'),

    # API для ККТ
    path('crm/client/<int:client_id>/kkt/add/', views.crm_kkt_add, name='crm_kkt_add'),
    path('crm/client/<int:client_id>/kkt/<int:kkt_id>/delete/', views.crm_kkt_delete, name='crm_kkt_delete'),
    path('crm/client/<int:client_id>/kkt/<int:kkt_id>/update/', views.crm_kkt_update, name='crm_kkt_update'),

    # Компании
    path('crm/companies/', views.crm_companies_list, name='crm_companies'),
    path('crm/company/create/', views.crm_company_create, name='crm_company_create'),
    path('crm/company/<int:company_id>/', views.crm_company_detail, name='crm_company_detail'),
    path('crm/company/<int:company_id>/edit/', views.crm_company_edit, name='crm_company_edit'),
    path('crm/company/<int:company_id>/delete/', views.crm_company_delete, name='crm_company_delete'),
    path('crm/company/<int:company_id>/contact/add/', views.crm_company_contact_add, name='crm_company_contact_add'),
    path('crm/company/<int:company_id>/contact/<int:contact_id>/remove/', views.crm_company_contact_remove, name='crm_company_contact_remove'),
    path('crm/company/<int:company_id>/kkt/add/', views.crm_company_kkt_add, name='crm_company_kkt_add'),
    path('crm/company/<int:company_id>/kkt/<int:kkt_id>/delete/', views.crm_company_kkt_delete, name='crm_company_kkt_delete'),
    path('crm/company/<int:company_id>/kkt/<int:kkt_id>/update/', views.crm_company_kkt_update, name='crm_company_kkt_update'),
    path('api/contacts/search/', views.api_contacts_search, name='api_contacts_search'),
    path('api/partner/contacts/search/', views.api_partner_contacts_search, name='api_partner_contacts_search'),
    path('api/clients/search/', views.api_clients_search, name='api_clients_search'),

    # API для заметок/сделок клиента
    path('crm/client/<int:client_id>/notes/', views.api_client_notes, name='api_client_notes'),
    path('crm/client/<int:client_id>/note/save/', views.api_client_note_save, name='api_client_note_save'),
    path('crm/client/<int:client_id>/note/<int:note_id>/delete/', views.api_client_note_delete, name='api_client_note_delete'),

    # API для сообщений чата
    path('api/chat-messages/<int:chat_id>/', views.api_chat_messages, name='api_chat_messages'),

    # Система уведомлений
    path('notifications/', views.notifications_list, name='notifications'),
    path('notifications/renewal/', views.notifications_renewal, name='notifications_renewal'),
    path('notifications/calendar/', views.notifications_calendar, name='notifications_calendar'),
    path('api/notification/<int:notification_id>/read/', views.api_notification_read, name='api_notification_read'),
    path('api/notification/<int:notification_id>/dismiss/', views.api_notification_dismiss, name='api_notification_dismiss'),
    path('api/notifications/read-all/', views.api_notifications_read_all, name='api_notifications_read_all'),
    path('api/notifications/count/', views.api_notifications_count, name='api_notifications_count'),

    # Bot API (Bearer token auth — приём данных от ботов)
    path('api/sales/', views.api_receive_sale, name='api_receive_sale'),
    path('api/applications/', views.api_receive_application, name='api_receive_application'),
    path('api/verifications/', views.api_receive_verification, name='api_receive_verification'),

    # События календаря
    path('api/calendar/events/', views.api_calendar_events, name='api_calendar_events'),
    path('api/calendar/event/save/', views.api_calendar_event_save, name='api_calendar_event_save'),
    path('api/calendar/event/<int:event_id>/delete/', views.api_calendar_event_delete, name='api_calendar_event_delete'),
    path('api/calendar/categories/', views.api_calendar_categories, name='api_calendar_categories'),

    # Задачи
    path('crm/tasks/', views.crm_tasks_list, name='crm_tasks'),
    path('api/tasks/export/', views.export_tasks_excel, name='export_tasks_excel'),
    path('api/tasks/stats/', views.api_tasks_stats, name='api_tasks_stats'),
    path('api/tasks/create/', views.api_task_create, name='api_task_create'),
    path('api/tasks/<int:task_id>/update/', views.api_task_update, name='api_task_update'),
    path('api/tasks/<int:task_id>/delete/', views.api_task_delete, name='api_task_delete'),

    # Финансовая модель — директор
    path('loyalty/dashboard/', views_loyalty.financial_dashboard_page, name='financial_dashboard'),
    path('loyalty/policies/', views_loyalty.loyalty_policies_page, name='loyalty_policies'),
    path('loyalty/partners/', views_loyalty.partner_finance_page, name='partner_finance'),
    path('loyalty/journal/', views_loyalty.loyalty_journal_page, name='loyalty_journal'),
    path('api/partner/set-rate/', views_loyalty.api_partner_set_rate, name='api_partner_set_rate'),
    path('api/partner/reward/', views_loyalty.api_partner_reward, name='api_partner_reward'),
    path('api/partner/complaint/', views_loyalty.api_partner_complaint, name='api_partner_complaint'),
    path('api/partner/adjust-rating/', views_loyalty.api_partner_adjust_rating, name='api_partner_adjust_rating'),
    path('api/salary/recalculate/', views_loyalty.api_salary_recalculate, name='api_salary_recalculate'),
    path('api/salary/approve/', views_loyalty.api_salary_approve, name='api_salary_approve'),
    path('api/salary/mark-paid/', views_loyalty.api_salary_mark_paid, name='api_salary_mark_paid'),
    path('api/salary/approve-all/', views_loyalty.api_salary_approve_all, name='api_salary_approve_all'),

    # Ручные операции с баллами (ЭТАП 3)
    path('loyalty/manual-operations/', views_loyalty.manual_operations_page, name='manual_operations'),
    path('api/manual/accrual/', views_loyalty.api_manual_accrual, name='api_manual_accrual'),
    path('api/manual/debit/', views_loyalty.api_manual_debit, name='api_manual_debit'),
    path('api/manual/adjust-rating/', views_loyalty.api_manual_adjust_rating, name='api_manual_adjust_rating'),
    path('api/search-client/', views_loyalty.api_search_client, name='api_search_client'),
    path('api/manual/history/', views_loyalty.api_manual_history, name='api_manual_history'),

    # Аналитика лояльности (ЭТАП 5)
    path('loyalty/analytics/', views_loyalty.loyalty_analytics_page, name='loyalty_analytics'),
    path('api/analytics/clients/', views_loyalty.api_analytics_clients, name='api_analytics_clients'),
    path('api/analytics/operations/', views_loyalty.api_analytics_operations, name='api_analytics_operations'),
    path('api/analytics/referrals/', views_loyalty.api_analytics_referrals, name='api_analytics_referrals'),
    path('api/analytics/export/', views_loyalty.api_analytics_export, name='api_analytics_export'),

    # Финансовая модель — кабинет партнёра
    path('my-finance/', views_loyalty.partner_cabinet_page, name='partner_cabinet'),
]
