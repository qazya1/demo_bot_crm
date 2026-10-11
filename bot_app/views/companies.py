"""CRM company views extracted from the legacy views module."""

import json
from datetime import date

from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from ..permissions import (
    crm_access_required,
    director_required,
    get_current_user,
    login_required_custom,
)
from ..services.company_service import (
    add_company_contact,
    create_company_from_payload,
    add_company_kkt,
    delete_company,
    delete_company_kkt,
    get_companies_list_context_data,
    get_company_detail_data,
    get_company_orders,
    remove_company_contact,
    search_contacts_for_company,
    update_company_from_payload,
    update_company_kkt,
)
from ..sql_cursor import with_sql_rewrite
from .company_schema import _ensure_companies_tables
from .dashboard_helpers import _get_all_specialists
from .orders import _ensure_orders_manual_columns

connection = with_sql_rewrite(connection)


@ensure_csrf_cookie
@login_required_custom
@crm_access_required
def crm_companies_list(request):
    """Список компаний"""
    _ensure_companies_tables()
    user = get_current_user(request)
    search = request.GET.get('search', '')
    context = get_companies_list_context_data(user, {'search': search})

    return render(request, 'bot_app/crm/companies_list.html', context)

@login_required_custom
@crm_access_required
def crm_company_create(request):
    """Создание новой компании"""
    _ensure_companies_tables()
    user = get_current_user(request)
    errors = {}
    form_data = {}

    if request.method == 'POST':
        data = {
            'name': request.POST.get('name', ''),
            'inn': request.POST.get('inn', ''),
            'kpp': request.POST.get('kpp', ''),
            'ogrn': request.POST.get('ogrn', ''),
            'address': request.POST.get('address', ''),
            'phone': request.POST.get('phone', ''),
            'email': request.POST.get('email', ''),
            'website': request.POST.get('website', ''),
            'description': request.POST.get('description', ''),
            'note': request.POST.get('note', ''),
        }
        result = create_company_from_payload(user, data)
        form_data = result.get('form_data', {})

        if result.get('success'):
            return redirect('bot_app:crm_company_detail', company_id=result['company_id'])

        errors = result.get('errors', {})

    return render(request, 'bot_app/crm/company_create.html', {
        'user': user,
        'errors': errors,
        'form_data': form_data,
    })

@ensure_csrf_cookie
@login_required_custom
@crm_access_required
def crm_company_detail(request, company_id):
    """Карточка компании"""
    _ensure_companies_tables()
    user = get_current_user(request)

    detail_data = get_company_detail_data(company_id, user, include_orders=False)
    if not detail_data:
        return render(request, 'bot_app/crm/client_not_found.html')

    _ensure_orders_manual_columns()
    detail_data['orders'] = get_company_orders(company_id)

    return render(request, 'bot_app/crm/company_detail.html', {
        **detail_data,
        'active_menu': 'companies',
        'today': date.today(),
        'user': user,
        'partner_mode': False,
        'companies_list_url': '/crm/companies/',
        'company_api_base': '/crm/company/',
        'contacts_search_url': '/api/contacts/search/',
        'can_edit_specialist': bool(user.get('is_director')),
        'specialists_for_edit': _get_all_specialists() if user.get('is_director') else [],
    })

@login_required_custom
@director_required
@crm_access_required
@require_POST
def crm_company_edit(request, company_id):
    """API для редактирования полей компании"""
    _ensure_companies_tables()
    user = get_current_user(request)
    try:
        data = json.loads(request.body)
        return JsonResponse(update_company_from_payload(user, company_id, data))

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Неверный формат данных'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@login_required_custom
@director_required
@crm_access_required
@require_POST
def crm_company_contact_add(request, company_id):
    """API: привязать контакт к компании (из существующих или создать нового)"""
    _ensure_companies_tables()
    try:
        data = json.loads(request.body)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

    return JsonResponse(add_company_contact(None, company_id, data))

@login_required_custom
@director_required
@crm_access_required
@require_POST
def crm_company_contact_remove(request, company_id, contact_id):
    """API: отвязать контакт от компании"""
    _ensure_companies_tables()
    return JsonResponse(remove_company_contact(None, company_id, contact_id))

@login_required_custom
@director_required
@crm_access_required
@require_POST
def crm_company_kkt_add(request, company_id):
    """Добавить ККТ в компанию"""
    _ensure_companies_tables()
    try:
        data = json.loads(request.body)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

    return JsonResponse(add_company_kkt(None, company_id, data))

@login_required_custom
@director_required
@crm_access_required
@require_POST
def crm_company_kkt_delete(request, company_id, kkt_id):
    """Удалить ККТ компании"""
    _ensure_companies_tables()
    return JsonResponse(delete_company_kkt(None, company_id, kkt_id))

@login_required_custom
@director_required
@crm_access_required
@require_POST
def crm_company_kkt_update(request, company_id, kkt_id):
    """Обновить поле ККТ компании"""
    _ensure_companies_tables()
    try:
        data = json.loads(request.body)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

    return JsonResponse(update_company_kkt(None, company_id, kkt_id, data))

@login_required_custom
@director_required
@crm_access_required
@require_POST
def crm_company_delete(request, company_id):
    """API: удалить компанию"""
    _ensure_companies_tables()
    payload, status = delete_company(None, company_id)
    if status is not None:
        return JsonResponse(payload, status=status)
    return JsonResponse(payload)

@login_required_custom
@crm_access_required
def api_contacts_search(request):
    """API: поиск контактов для автокомплита при добавлении в компанию"""
    q = request.GET.get('q', '').strip()
    return JsonResponse(search_contacts_for_company(q))
