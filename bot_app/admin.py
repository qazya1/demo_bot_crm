"""
Админка БД бота - точное соответствие new_fulldump.sql
Организовано по бизнес-доменам
"""
from django.contrib import admin
from django.utils.html import format_html
from datetime import date
from .models import (
    Users, ClientKkt,
    Orders, OrderItems, Codes, Equipment,
    SupportChats, SupportChatMessages, SupportChatFiles, SupportChatOffers,
    SupportChatDeclines, SupportChatPayments, SupportChatTokens,
    Specialists, SpecialistPromocodes,
    ReferralCodes, ReferralActivations,
    ExtProductOperator, ExtProductType, ExtProduct, BotEvents, UiState, OfdSubscriptions
)

admin.site.site_header = "CRM Бот - Панель управления"
admin.site.site_title = "CRM Бот"
admin.site.index_title = "База данных"


# ==============================================================================
# ПОЛЬЗОВАТЕЛИ / КЛИЕНТЫ
# ==============================================================================

@admin.register(Users)
class UsersAdmin(admin.ModelAdmin):
    """Пользователи - точно как в БД (new_fulldump.sql)"""
    # Все поля в порядке как в БД: id, tg_user_id, first_name, last_name, phone, email,
    # tg_username, inn, org_name, kpp, city, amo_company_id, we_added, consent_accepted,
    # consent_accepted_at, is_verified, created_at, updated_at
    list_display = (
        'id', 'tg_user_id', 'first_name', 'last_name', 'phone', 'email',
        'tg_username', 'inn', 'org_name', 'kpp', 'city', 'amo_company_id', 'we_added',
        'consent_accepted', 'consent_accepted_at', 'is_verified',
        'created_at', 'updated_at'
    )
    list_display_links = ('id', 'tg_user_id')
    search_fields = ('id', 'tg_user_id', 'first_name', 'last_name', 'phone', 'email',
                     'tg_username', 'inn', 'org_name', 'city')
    list_filter = ('we_added', 'is_verified', 'consent_accepted', 'city', 'created_at')
    readonly_fields = ('id',)
    list_per_page = 50
    ordering = ('-id',)

    # Поля для редактирования в том же порядке как в БД
    fields = (
        'id', 'tg_user_id', 'first_name', 'last_name', 'phone', 'email',
        'tg_username', 'inn', 'org_name', 'kpp', 'city', 'amo_company_id', 'we_added',
        'consent_accepted', 'consent_accepted_at', 'is_verified',
        'created_at', 'updated_at'
    )


# ==============================================================================
# ОБОРУДОВАНИЕ КЛИЕНТОВ (ККТ, ФН, ОФД, МАРКИРОВКА)
# ==============================================================================

@admin.register(ClientKkt)
class ClientKktAdmin(admin.ModelAdmin):
    list_display = ('id', 'client_id', 'kkt_slot', 'address_short', 'fn_days_left', 'ofd_days_left', 'mark_days_left', 'created_at')
    search_fields = ('id', 'client_id', 'kkt_slot', 'address')
    list_filter = ('fn_expires_at', 'ofd_expires_at', 'mark_expires_at', 'created_at')
    ordering = ('-id',)

    def address_short(self, obj):
        if obj.address:
            return (obj.address[:50] + "...") if len(obj.address) > 50 else obj.address
        return "—"
    address_short.short_description = "Адрес"

    def _days_left_html(self, dt):
        if not dt:
            return "—"
        delta = (dt.date() - date.today()).days
        if delta < 0:
            return format_html('<span style="color: red;">Истёк ({} дн)</span>', abs(delta))
        elif delta < 30:
            return format_html('<span style="color: orange;">{} дн</span>', delta)
        return format_html('<span style="color: green;">{} дн</span>', delta)

    def fn_days_left(self, obj):
        return self._days_left_html(obj.fn_expires_at)
    fn_days_left.short_description = "ФН"

    def ofd_days_left(self, obj):
        return self._days_left_html(obj.ofd_expires_at)
    ofd_days_left.short_description = "ОФД"

    def mark_days_left(self, obj):
        return self._days_left_html(obj.mark_expires_at)
    mark_days_left.short_description = "Маркировка"


# ==============================================================================
# ЗАКАЗЫ И КОММЕРЦИЯ
# ==============================================================================

@admin.register(Orders)
class OrdersAdmin(admin.ModelAdmin):
    list_display = ('id', 'user_id', 'status_badge', 'amount_rub', 'payment_id', 'paid_at', 'created_at')
    search_fields = ('id', 'user_id', 'payment_id')
    list_filter = ('status', 'paid_at', 'created_at')
    readonly_fields = ('id', 'created_at', 'updated_at', 'paid_at')
    list_per_page = 50
    ordering = ('-id',)

    def status_badge(self, obj):
        colors = {
            'paid': ('green', 'Оплачен'),
            'pending': ('orange', 'Ожидает'),
            'cancelled': ('red', 'Отменён'),
            'refunded': ('purple', 'Возврат'),
            'new': ('blue', 'Новый'),
        }
        color, text = colors.get(obj.status, ('gray', obj.status or '—'))
        return format_html('<span style="color: {};">{}</span>', color, text)
    status_badge.short_description = "Статус"

    def amount_rub(self, obj):
        return f"{obj.amount} руб"
    amount_rub.short_description = "Сумма"


@admin.register(OrderItems)
class OrderItemsAdmin(admin.ModelAdmin):
    list_display = ('id', 'order_id', 'code_id', 'equipment_id', 'qty', 'price_rub', 'total_rub')
    search_fields = ('id', 'order_id', 'code_id', 'equipment_id')
    list_filter = ('created_at',)
    ordering = ('-id',)

    def price_rub(self, obj):
        return f"{obj.price} руб"
    price_rub.short_description = "Цена"

    def total_rub(self, obj):
        return f"{obj.qty * obj.price} руб"
    total_rub.short_description = "Итого"


@admin.register(Codes)
class CodesAdmin(admin.ModelAdmin):
    list_display = ('id', 'code_short', 'code_type', 'is_used_badge', 'user_id', 'vendor_name', 'prices', 'expires_at')
    search_fields = ('id', 'code_value', 'vendor_name', 'user_id')
    list_filter = ('code_type', 'is_used')
    list_per_page = 50
    ordering = ('-id',)

    def code_short(self, obj):
        if len(obj.code_value) > 25:
            return obj.code_value[:25] + "..."
        return obj.code_value
    code_short.short_description = "Код"

    def is_used_badge(self, obj):
        if obj.is_used:
            return format_html('<span style="color: green;">Да</span>')
        return format_html('<span style="color: gray;">Нет</span>')
    is_used_badge.short_description = "Использован"

    def prices(self, obj):
        if obj.source_price and obj.sell_price:
            profit = obj.sell_price - obj.source_price
            color = 'green' if profit > 0 else 'red'
            return format_html('{} -> {} (<span style="color: {};">+{}</span>)',
                             obj.source_price, obj.sell_price, color, profit)
        return "—"
    prices.short_description = "Цены"


@admin.register(Equipment)
class EquipmentAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'category', 'is_used_badge', 'price_rub', 'count')
    search_fields = ('id', 'name')
    list_filter = ('category', 'is_used')
    ordering = ('category', 'name')

    def is_used_badge(self, obj):
        if obj.is_used:
            return format_html('<span style="color: orange;">Б/У</span>')
        return "Новое"
    is_used_badge.short_description = "Состояние"

    def price_rub(self, obj):
        return f"{obj.price} руб"
    price_rub.short_description = "Цена"


# ==============================================================================
# ПОДДЕРЖКА
# ==============================================================================

@admin.register(SupportChats)
class SupportChatsAdmin(admin.ModelAdmin):
    list_display = ('id', 'client_user_id', 'specialist_id', 'status_badge', 'profession_name', 'category', 'created_at')
    search_fields = ('id', 'client_user_id', 'category', 'request_type')
    list_filter = ('status', 'profession', 'created_at')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('-id',)

    def status_badge(self, obj):
        colors = {
            'pending': ('orange', 'Ожидает'),
            'active': ('green', 'Активен'),
            'consultation': ('blue', 'Консультация'),
            'closed': ('gray', 'Закрыт'),
        }
        color, text = colors.get(obj.status, ('gray', obj.status))
        return format_html('<span style="color: {};">{}</span>', color, text)
    status_badge.short_description = "Статус"

    def profession_name(self, obj):
        profs = {
            0: "Бухгалтер",
            1: "Тех. спец.",
            2: "Директор",
            3: "Консультант",
            4: "Разрешительная документация",
        }
        return profs.get(obj.profession, f"#{obj.profession}")
    profession_name.short_description = "Профессия"


@admin.register(SupportChatMessages)
class SupportChatMessagesAdmin(admin.ModelAdmin):
    list_display = ('id', 'chat_id', 'sender_name', 'text_short', 'created_at')
    search_fields = ('id', 'chat_id', 'text')
    list_filter = ('sender', 'created_at')
    ordering = ('-id',)

    def sender_name(self, obj):
        if obj.sender == 0:
            return format_html('<span style="color: blue;">Клиент</span>')
        return format_html('<span style="color: green;">Специалист</span>')
    sender_name.short_description = "Отправитель"

    def text_short(self, obj):
        return (obj.text[:80] + "...") if len(obj.text) > 80 else obj.text
    text_short.short_description = "Сообщение"


@admin.register(SupportChatFiles)
class SupportChatFilesAdmin(admin.ModelAdmin):
    list_display = ('id', 'chat_id', 'sender', 'original_name', 'mime', 'size_kb', 'created_at')
    search_fields = ('id', 'chat_id', 'original_name')
    list_filter = ('mime', 'sender')
    ordering = ('-id',)

    def size_kb(self, obj):
        if obj.size_bytes > 1024 * 1024:
            return f"{obj.size_bytes / 1024 / 1024:.1f} МБ"
        return f"{obj.size_bytes / 1024:.1f} КБ"
    size_kb.short_description = "Размер"


@admin.register(SupportChatOffers)
class SupportChatOffersAdmin(admin.ModelAdmin):
    """Только просмотр - составной первичный ключ"""
    list_display = ('chat_id', 'specialist_id', 'tg_chat_id', 'message_id', 'created_at')
    list_display_links = None  # Отключаем ссылки для редактирования
    search_fields = ('chat_id', 'specialist_id')
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SupportChatDeclines)
class SupportChatDeclinesAdmin(admin.ModelAdmin):
    """Только просмотр - составной первичный ключ"""
    list_display = ('chat_id', 'specialist_id', 'created_at')
    list_display_links = None  # Отключаем ссылки для редактирования
    search_fields = ('chat_id', 'specialist_id')
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SupportChatPayments)
class SupportChatPaymentsAdmin(admin.ModelAdmin):
    list_display = ('id', 'chat_id', 'specialist_id', 'amount_rub', 'status_badge', 'created_at')
    search_fields = ('id', 'chat_id', 'payment_id')
    list_filter = ('status',)
    ordering = ('-id',)

    def amount_rub(self, obj):
        return f"{obj.amount_rub} руб"
    amount_rub.short_description = "Сумма"

    def status_badge(self, obj):
        colors = {'NEW': 'gray', 'PENDING': 'orange', 'PAID': 'green'}
        return format_html('<span style="color: {};">{}</span>',
                          colors.get(obj.status, 'gray'), obj.status)
    status_badge.short_description = "Статус"


@admin.register(SupportChatTokens)
class SupportChatTokensAdmin(admin.ModelAdmin):
    list_display = ('id', 'chat_id', 'role', 'token_short', 'created_at', 'expires_at')
    search_fields = ('id', 'chat_id', 'token', 'actor_id')
    list_filter = ('role',)
    ordering = ('-id',)

    def token_short(self, obj):
        return obj.token[:20] + "..." if len(obj.token) > 20 else obj.token
    token_short.short_description = "Токен"


# ==============================================================================
# СПЕЦИАЛИСТЫ
# ==============================================================================

@admin.register(Specialists)
class SpecialistsAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'profession_name', 'role', 'phone', 'tg_username', 'is_active_badge', 'phone_verified_badge')
    search_fields = ('id', 'name', 'phone', 'tg_username', 'tg_user_id')
    list_filter = ('is_active', 'profession', 'role', 'phone_verified')
    ordering = ('name',)

    def profession_name(self, obj):
        return obj.get_profession_display()
    profession_name.short_description = "Профессия"

    def is_active_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color: green;">Да</span>')
        return format_html('<span style="color: red;">Нет</span>')
    is_active_badge.short_description = "Активен"

    def phone_verified_badge(self, obj):
        if obj.phone_verified:
            return format_html('<span style="color: green;">Да</span>')
        return format_html('<span style="color: gray;">Нет</span>')
    phone_verified_badge.short_description = "Тел. подтв."


@admin.register(SpecialistPromocodes)
class SpecialistPromocodesAdmin(admin.ModelAdmin):
    list_display = ('id', 'promo_code', 'specialist_id', 'created_at')
    search_fields = ('id', 'promo_code', 'specialist_id')
    ordering = ('-id',)


# ==============================================================================
# РЕФЕРАЛЬНАЯ СИСТЕМА
# ==============================================================================

@admin.register(ReferralCodes)
class ReferralCodesAdmin(admin.ModelAdmin):
    list_display = ('id', 'code', 'owner_name', 'owner_note', 'is_active_badge', 'created_at')
    search_fields = ('id', 'code', 'owner_name')
    list_filter = ('is_active',)
    ordering = ('-id',)

    def is_active_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color: green;">Активен</span>')
        return format_html('<span style="color: red;">Неактивен</span>')
    is_active_badge.short_description = "Статус"


@admin.register(ReferralActivations)
class ReferralActivationsAdmin(admin.ModelAdmin):
    list_display = ('id', 'user_id', 'referral_code_id', 'activated_at', 'raw_text')
    search_fields = ('id', 'user_id', 'raw_text')
    list_filter = ('activated_at',)
    ordering = ('-id',)


# ==============================================================================
# ЦЕНООБРАЗОВАНИЕ
# ==============================================================================

@admin.register(ExtProductOperator)
class ExtProductOperatorAdmin(admin.ModelAdmin):
    list_display = ('id', 'category', 'operator_id', 'name', 'is_active')
    list_filter = ('category', 'is_active')
    ordering = ('category', 'sort_order', 'operator_id')


@admin.register(ExtProductType)
class ExtProductTypeAdmin(admin.ModelAdmin):
    list_display = ('id', 'type_id', 'name', 'months_number', 'is_active')
    list_filter = ('is_active',)
    ordering = ('sort_order', 'type_id')


@admin.register(ExtProduct)
class ExtProductAdmin(admin.ModelAdmin):
    list_display = ('id', 'category', 'operator_id', 'type_id', 'source_price', 'price_fixed', 'markup_pct', 'is_active_badge')
    list_filter = ('category', 'is_active')
    ordering = ('category', 'operator_id', 'type_id')

    def is_active_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="color: green;">Да</span>')
        return format_html('<span style="color: red;">Нет</span>')
    is_active_badge.short_description = "Активен"


# ==============================================================================
# АНАЛИТИКА БОТА
# ==============================================================================

@admin.register(BotEvents)
class BotEventsAdmin(admin.ModelAdmin):
    list_display = ('id', 'ts', 'event_type', 'name', 'user_id', 'callback_short')
    search_fields = ('id', 'user_id', 'event_type', 'name', 'callback_data')
    list_filter = ('event_type', 'ts', 'has_file')
    readonly_fields = ('id', 'ts')
    list_per_page = 100
    ordering = ('-id',)

    def callback_short(self, obj):
        if obj.callback_data:
            return (obj.callback_data[:40] + "...") if len(obj.callback_data) > 40 else obj.callback_data
        return "—"
    callback_short.short_description = "Callback"


@admin.register(UiState)
class UiStateAdmin(admin.ModelAdmin):
    list_display = ('tg_user_id', 'main_menu_mid', 'boot_notice_mid', 'updated_at')
    search_fields = ('tg_user_id',)
    ordering = ('-updated_at',)


# ==============================================================================
# LEGACY
# ==============================================================================

@admin.register(OfdSubscriptions)
class OfdSubscriptionsAdmin(admin.ModelAdmin):
    list_display = ('id', 'client_id', 'kkt_slot', 'ofd_expires_at', 'source', 'created_at')
    search_fields = ('id', 'client_id', 'kkt_slot')
    list_filter = ('ofd_expires_at',)
    ordering = ('-id',)
