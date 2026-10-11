from django import template

register = template.Library()

LOYALTY_VALUE_LABELS = {
    'product_type': {
        'order': 'Заказ',
        'service': 'Услуга',
        'ofd': 'ОФД',
        'edo': 'ЭДО',
        'kkt': 'ККТ',
        'chat_payment': 'Оплата в чате',
    },
    'policy_event_type': {
        'purchase': 'Покупка',
        'first_purchase': 'Первая покупка',
        'repeat_purchase': 'Повторная покупка',
        'profile_field': 'Заполнение профиля',
    },
    'target_type': {
        'client': 'Клиент',
        'partner': 'Партнёр',
    },
    'tier_name': {
        'bronze': 'Бронза',
        'silver': 'Серебро',
        'gold': 'Золото',
        'platinum': 'Платина',
        'standard': 'Стандарт',
        'advanced': 'Продвинутый',
        'expert': 'Эксперт',
        'top': 'Топ',
    },
}


@register.filter
def get_item(dictionary, key):
    """Получить элемент словаря по ключу в шаблоне."""
    if dictionary is None:
        return None
    return dictionary.get(key)


@register.filter
def priority_class(priority):
    """Вернуть CSS класс для приоритета."""
    return f"priority-{priority}" if priority else "priority-medium"


@register.filter
def loyalty_label(value, kind):
    """Локализованный лейбл для кодовых значений лояльности."""
    if value is None:
        return '—'
    text_value = str(value)
    labels = LOYALTY_VALUE_LABELS.get(kind, {})
    return labels.get(text_value, text_value)
