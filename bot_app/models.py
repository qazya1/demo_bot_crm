"""
Модели БД бота - точное соответствие new_fulldump.sql
Организованы по бизнес-доменам
"""
from django.db import models


# ==============================================================================
# ПОЛЬЗОВАТЕЛИ / КЛИЕНТЫ
# ==============================================================================

class Users(models.Model):
    """Пользователи бота (основная таблица клиентов)"""
    id = models.AutoField(primary_key=True)
    tg_user_id = models.BigIntegerField(unique=True, null=True, blank=True, verbose_name="Telegram ID")
    max_user_id = models.BigIntegerField(unique=True, null=True, blank=True, verbose_name="MAX Messenger ID")
    first_name = models.CharField(max_length=255, null=True, blank=True, verbose_name="Имя")
    last_name = models.CharField(max_length=255, null=True, blank=True, verbose_name="Фамилия")
    phone = models.CharField(max_length=50, null=True, blank=True, db_index=True, verbose_name="Телефон")
    email = models.CharField(max_length=255, null=True, blank=True, verbose_name="Email")
    tg_username = models.CharField(max_length=255, null=True, blank=True, verbose_name="TG username")
    inn = models.CharField(max_length=20, null=True, blank=True, db_index=True, verbose_name="ИНН")
    org_name = models.CharField(max_length=255, null=True, blank=True, verbose_name="Название организации")
    kpp = models.CharField(max_length=20, null=True, blank=True, verbose_name="КПП")
    city = models.CharField(max_length=255, null=True, blank=True, verbose_name="Город")
    address = models.TextField(null=True, blank=True, verbose_name="Адрес")
    amo_company_id = models.CharField(max_length=64, null=True, blank=True, verbose_name="AMO Company ID")
    we_added = models.BooleanField(default=False, verbose_name="Добавлен в базу клиентов")
    consent_accepted = models.BooleanField(default=False, verbose_name="Согласие принято")
    consent_accepted_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата согласия")
    is_verified = models.BooleanField(default=False, verbose_name="Верифицирован")
    created_at = models.DateTimeField(null=True, blank=True, verbose_name="Создан")
    updated_at = models.DateTimeField(null=True, blank=True, verbose_name="Обновлён")

    class Meta:
        managed = False
        db_table = 'users'
        verbose_name = "Пользователь"
        verbose_name_plural = "Пользователи"

    def __str__(self):
        name = f"{self.first_name or ''} {self.last_name or ''}".strip()
        if name:
            return f"{name} ({self.inn or self.phone or self.id})"
        return f"User #{self.id} ({self.inn or self.phone or ''})"


# ==============================================================================
# ОБОРУДОВАНИЕ КЛИЕНТОВ (ККТ, ФН, ОФД, МАРКИРОВКА)
# ==============================================================================

class ClientKkt(models.Model):
    """ККТ клиентов (торговые точки)"""
    id = models.AutoField(primary_key=True)
    client_id = models.IntegerField(db_index=True, verbose_name="ID клиента")  # FK -> users.id
    kkt_slot = models.CharField(max_length=50, verbose_name="Слот ККТ")
    address = models.TextField(null=True, blank=True, verbose_name="Адрес")
    fn_expires_at = models.DateTimeField(null=True, blank=True, verbose_name="ФН истекает")
    ofd_expires_at = models.DateTimeField(null=True, blank=True, verbose_name="ОФД истекает")
    mark_expires_at = models.DateTimeField(null=True, blank=True, verbose_name="Маркировка истекает")
    created_at = models.DateTimeField(null=True, blank=True, verbose_name="Создано")
    updated_at = models.DateTimeField(null=True, blank=True, verbose_name="Обновлено")

    class Meta:
        managed = False
        db_table = 'client_kkt'
        verbose_name = "ККТ"
        verbose_name_plural = "ККТ клиентов"
        unique_together = [['client_id', 'kkt_slot']]

    def __str__(self):
        return f"ККТ #{self.id} ({self.kkt_slot})"


# Сроки ФН/ОФД/Маркировки хранятся прямо в client_kkt
# (поля fn_expires_at, ofd_expires_at, mark_expires_at)


# ==============================================================================
# ЗАКАЗЫ И КОММЕРЦИЯ
# ==============================================================================

class Orders(models.Model):
    """Сделки"""
    id = models.AutoField(primary_key=True)
    user_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name="ID пользователя")  # FK -> users.id
    company_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name="ID компании")  # FK -> companies.id
    specialist_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name="ID специалиста")  # FK -> specialists.id
    amount = models.IntegerField(default=0, verbose_name="Сумма")
    status = models.CharField(max_length=50, verbose_name="Статус")
    source = models.CharField(max_length=32, null=True, blank=True, verbose_name="Источник")  # telegram / max / manual
    payment_info = models.TextField(null=True, blank=True, verbose_name="Информация об оплате")
    description = models.TextField(null=True, blank=True, verbose_name="Описание")
    address = models.TextField(null=True, blank=True, verbose_name="Адрес доставки")
    author_name = models.CharField(max_length=255, null=True, blank=True, verbose_name="Автор")
    payment_id = models.CharField(max_length=255, null=True, blank=True, verbose_name="ID платежа")
    created_at = models.DateTimeField(null=True, blank=True, verbose_name="Создан")
    updated_at = models.DateTimeField(null=True, blank=True, verbose_name="Обновлён")
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name="Оплачен")

    class Meta:
        managed = False
        db_table = 'orders'
        verbose_name = "Сделка"
        verbose_name_plural = "Сделки"

    def __str__(self):
        return f"Сделка #{self.id}"


class OrderItems(models.Model):
    """Позиции заказов"""
    id = models.AutoField(primary_key=True)
    order_id = models.IntegerField(db_index=True, verbose_name="ID заказа")  # FK -> orders.id
    code_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name="ID кода")  # FK -> codes.id
    equipment_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name="ID оборудования")  # FK -> equipment.id
    qty = models.IntegerField(default=1, verbose_name="Количество")
    price = models.IntegerField(default=0, verbose_name="Цена")
    created_at = models.DateTimeField(null=True, blank=True, verbose_name="Создано")
    legacy_item_type = models.CharField(max_length=255, null=True, blank=True, verbose_name="Legacy тип")
    legacy_item_title = models.CharField(max_length=255, null=True, blank=True, verbose_name="Legacy название")

    class Meta:
        managed = False
        db_table = 'order_items'
        verbose_name = "Позиция заказа"
        verbose_name_plural = "Позиции заказов"

    def __str__(self):
        return f"Позиция #{self.id} (Заказ: {self.order_id})"


class Codes(models.Model):
    """Коды (ОФД, сервисы, ЭДО)"""
    CODE_TYPES = [
        ('ofd_first', 'ОФД первый'),
        ('ofd_platform', 'ОФД платформа'),
        ('ofd_other', 'ОФД другой'),
        ('service', 'Сервис'),
        ('edo', 'ЭДО'),
    ]

    id = models.AutoField(primary_key=True)
    code_value = models.CharField(max_length=255, verbose_name="Значение кода")
    is_used = models.BooleanField(default=False, verbose_name="Использован")
    user_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name="ID пользователя")  # FK -> users.id
    issued_at = models.DateTimeField(null=True, blank=True, verbose_name="Выдан")
    code_type = models.CharField(max_length=50, choices=CODE_TYPES, null=True, blank=True, verbose_name="Тип кода")
    operator_id = models.IntegerField(null=True, blank=True, verbose_name="ID оператора")
    type_id = models.IntegerField(null=True, blank=True, verbose_name="ID типа")
    source_price = models.IntegerField(null=True, blank=True, verbose_name="Цена закупки")
    sell_price = models.IntegerField(null=True, blank=True, verbose_name="Цена продажи")
    vendor_name = models.CharField(max_length=255, null=True, blank=True, verbose_name="Вендор")
    months_number = models.IntegerField(null=True, blank=True, verbose_name="Месяцев")
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name="Истекает")
    legacy_code_type_text = models.CharField(max_length=255, null=True, blank=True, verbose_name="Legacy тип текст")

    class Meta:
        managed = False
        db_table = 'codes'
        verbose_name = "Код"
        verbose_name_plural = "Коды"

    def __str__(self):
        return f"{self.code_value[:20]}..." if len(self.code_value) > 20 else self.code_value


class Equipment(models.Model):
    """Оборудование"""
    CATEGORIES = [
        ('Кассы', 'Кассы'),
        ('Сканер', 'Сканер'),
        ('ФН', 'ФН'),
        ('Кассовые ленты', 'Кассовые ленты'),
        ('Этикетки', 'Этикетки'),
    ]

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=100, verbose_name="Название")
    category = models.CharField(max_length=50, choices=CATEGORIES, verbose_name="Категория")
    is_used = models.BooleanField(default=False, verbose_name="Б/У")
    price = models.IntegerField(default=0, verbose_name="Цена")
    image_name = models.CharField(max_length=100, null=True, blank=True, verbose_name="Изображение")
    count = models.IntegerField(null=True, blank=True, verbose_name="Количество")

    class Meta:
        managed = False
        db_table = 'equipment'
        verbose_name = "Оборудование"
        verbose_name_plural = "Оборудование"

    def __str__(self):
        return f"{self.name} ({self.category})"


# ==============================================================================
# ПОДДЕРЖКА
# ==============================================================================

class SupportChats(models.Model):
    """Чаты поддержки"""
    STATUS_CHOICES = [
        ('pending', 'Ожидает'),
        ('active', 'Активен'),
        ('consultation', 'Консультация'),
        ('closed', 'Закрыт'),
    ]

    id = models.BigAutoField(primary_key=True)
    client_user_id = models.BigIntegerField(verbose_name="Internal user ID клиента")
    specialist_id = models.BigIntegerField(null=True, blank=True, verbose_name="ID специалиста")  # FK -> specialists.id
    profession = models.IntegerField(verbose_name="Профессия")
    category = models.CharField(max_length=128, null=True, blank=True, verbose_name="Категория")
    request_type = models.CharField(max_length=128, null=True, blank=True, verbose_name="Тип запроса")
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default='pending', verbose_name="Статус")
    source = models.CharField(max_length=32, null=True, blank=True, verbose_name="Источник")  # telegram / max
    created_at = models.DateTimeField(verbose_name="Создан")
    updated_at = models.DateTimeField(verbose_name="Обновлён")
    close_req_client = models.BooleanField(default=False, verbose_name="Запрос закрытия от клиента")
    close_req_agent = models.BooleanField(default=False, verbose_name="Запрос закрытия от агента")
    close_req_client_at = models.DateTimeField(null=True, blank=True, verbose_name="Время запроса клиента")
    close_req_agent_at = models.DateTimeField(null=True, blank=True, verbose_name="Время запроса агента")
    accepted_at = models.DateTimeField(null=True, blank=True, verbose_name="Время принятия специалистом")
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name="Время закрытия")

    class Meta:
        managed = False
        db_table = 'support_chats'
        verbose_name = "Чат поддержки"
        verbose_name_plural = "Чаты поддержки"

    def __str__(self):
        return f"Чат #{self.id} ({self.status})"


class SupportChatMessages(models.Model):
    """Сообщения чатов"""
    id = models.BigAutoField(primary_key=True)
    chat_id = models.BigIntegerField(db_index=True, verbose_name="ID чата")  # FK -> support_chats.id
    created_at = models.DateTimeField(verbose_name="Создано")
    sender = models.SmallIntegerField(verbose_name="Отправитель")  # 0 = клиент, 1 = специалист
    text = models.TextField(verbose_name="Текст")
    edited_at = models.DateTimeField(null=True, blank=True, verbose_name="Отредактировано")
    edited_by = models.BigIntegerField(null=True, blank=True, verbose_name="Кем отредактировано")
    is_edited = models.BooleanField(default=False, verbose_name="Было отредактировано")

    class Meta:
        managed = False
        db_table = 'support_chat_messages'
        verbose_name = "Сообщение"
        verbose_name_plural = "Сообщения чатов"

    def __str__(self):
        return f"Сообщение #{self.id}"


class SupportChatFiles(models.Model):
    """Файлы чатов"""
    id = models.BigAutoField(primary_key=True)
    chat_id = models.BigIntegerField(db_index=True, verbose_name="ID чата")
    message_id = models.BigIntegerField(null=True, blank=True, db_index=True, verbose_name="ID сообщения")
    sender = models.SmallIntegerField(verbose_name="Отправитель")
    original_name = models.CharField(max_length=255, verbose_name="Имя файла")
    stored_name = models.CharField(max_length=255, verbose_name="Сохранённое имя")
    mime = models.CharField(max_length=100, verbose_name="MIME тип")
    size_bytes = models.BigIntegerField(verbose_name="Размер (байт)")
    created_at = models.DateTimeField(null=True, blank=True, verbose_name="Создано")

    class Meta:
        managed = False
        db_table = 'support_chat_files'
        verbose_name = "Файл"
        verbose_name_plural = "Файлы чатов"

    def __str__(self):
        return self.original_name


class SupportChatOffers(models.Model):
    """Предложения специалистов - только для просмотра (составной PK)"""
    chat_id = models.BigIntegerField(primary_key=True, verbose_name="ID чата")
    specialist_id = models.BigIntegerField(verbose_name="ID специалиста")
    tg_chat_id = models.BigIntegerField(verbose_name="TG ID чата")
    message_id = models.BigIntegerField(verbose_name="ID сообщения")
    created_at = models.DateTimeField(verbose_name="Создано")

    class Meta:
        managed = False
        db_table = 'support_chat_offers'
        verbose_name = "Предложение"
        verbose_name_plural = "Предложения"

    def __str__(self):
        return f"Предложение (Чат: {self.chat_id}, Спец: {self.specialist_id})"


class SupportChatDeclines(models.Model):
    """Отказы специалистов - только для просмотра (составной PK)"""
    chat_id = models.BigIntegerField(primary_key=True, verbose_name="ID чата")
    specialist_id = models.BigIntegerField(verbose_name="ID специалиста")
    created_at = models.DateTimeField(verbose_name="Создано")

    class Meta:
        managed = False
        db_table = 'support_chat_declines'
        verbose_name = "Отказ"
        verbose_name_plural = "Отказы"

    def __str__(self):
        return f"Отказ (Чат: {self.chat_id}, Спец: {self.specialist_id})"


class SupportChatPayments(models.Model):
    """Платежи в чатах"""
    id = models.BigAutoField(primary_key=True)
    chat_id = models.BigIntegerField(db_index=True, verbose_name="ID чата")
    specialist_id = models.BigIntegerField(verbose_name="ID специалиста")
    amount_rub = models.IntegerField(verbose_name="Сумма (руб)")
    payment_id = models.CharField(max_length=64, unique=True, verbose_name="ID платежа")
    pay_url = models.TextField(verbose_name="Ссылка на оплату")
    status = models.CharField(max_length=32, default='NEW', verbose_name="Статус")
    created_at = models.DateTimeField(verbose_name="Создан")
    updated_at = models.DateTimeField(verbose_name="Обновлён")
    order_id = models.IntegerField(null=True, blank=True, db_index=True, verbose_name="ID заказа")  # FK -> orders.id

    class Meta:
        managed = False
        db_table = 'support_chat_payments'
        verbose_name = "Платёж"
        verbose_name_plural = "Платежи"

    def __str__(self):
        return f"Платёж #{self.id} ({self.amount_rub} руб)"


class SupportChatTokens(models.Model):
    """Токены чатов"""
    id = models.BigAutoField(primary_key=True)
    token = models.CharField(max_length=128, unique=True, verbose_name="Токен")
    chat_id = models.BigIntegerField(db_index=True, verbose_name="ID чата")
    role = models.CharField(max_length=16, verbose_name="Роль")
    actor_id = models.CharField(max_length=64, null=True, blank=True, verbose_name="ID актора")
    created_at = models.DateTimeField(verbose_name="Создан")
    expires_at = models.DateTimeField(db_index=True, verbose_name="Истекает")
    revoked_at = models.DateTimeField(null=True, blank=True, verbose_name="Отозван")
    replaced_by = models.BigIntegerField(null=True, blank=True, verbose_name="Заменён на")
    last_seen_at = models.DateTimeField(null=True, blank=True, verbose_name="Последняя активность")

    class Meta:
        managed = False
        db_table = 'support_chat_tokens'
        verbose_name = "Токен"
        verbose_name_plural = "Токены"

    def __str__(self):
        return f"Токен #{self.id} ({self.role})"


# ==============================================================================
# СПЕЦИАЛИСТЫ
# ==============================================================================

class Specialists(models.Model):
    """Специалисты"""
    PROFESSIONS = [
        (0, 'Бухгалтер'),
        (1, 'Тех. специалист'),
        (2, 'Директор'),
        (3, 'Консультант'),
        (4, 'Разрешительная документация'),
    ]

    id = models.BigAutoField(primary_key=True)
    phone = models.CharField(max_length=32, null=True, blank=True, verbose_name="Телефон")
    tg_username = models.CharField(max_length=64, null=True, blank=True, verbose_name="TG username")
    tg_user_id = models.BigIntegerField(unique=True, null=True, blank=True, verbose_name="TG ID")
    name = models.CharField(max_length=128, verbose_name="Имя")
    profession = models.SmallIntegerField(choices=PROFESSIONS, verbose_name="Профессия")
    role = models.CharField(max_length=32, default='specialist', verbose_name="Роль")
    is_active = models.BooleanField(default=True, verbose_name="Активен")
    created_at = models.DateTimeField(verbose_name="Создан")
    updated_at = models.DateTimeField(verbose_name="Обновлён")
    phone_verified = models.BooleanField(default=False, verbose_name="Телефон подтверждён")

    class Meta:
        managed = False
        db_table = 'specialists'
        verbose_name = "Специалист"
        verbose_name_plural = "Специалисты"

    def __str__(self):
        return f"{self.name} ({self.get_profession_display()})"


class SpecialistPromocodes(models.Model):
    """Промокоды специалистов"""
    id = models.BigAutoField(primary_key=True)
    promo_code = models.CharField(max_length=64, unique=True, verbose_name="Промокод")
    specialist_id = models.BigIntegerField(db_index=True, verbose_name="ID специалиста")  # FK -> specialists.id
    created_at = models.DateTimeField(verbose_name="Создан")

    class Meta:
        managed = False
        db_table = 'specialist_promocodes'
        verbose_name = "Промокод"
        verbose_name_plural = "Промокоды"

    def __str__(self):
        return self.promo_code


# ==============================================================================
# РЕФЕРАЛЬНАЯ СИСТЕМА
# ==============================================================================

class ReferralCodes(models.Model):
    """Реферальные коды"""
    id = models.BigAutoField(primary_key=True)
    code = models.CharField(max_length=32, unique=True, verbose_name="Код")
    owner_name = models.CharField(max_length=128, verbose_name="Владелец")
    owner_note = models.CharField(max_length=255, null=True, blank=True, verbose_name="Заметка")
    is_active = models.BooleanField(default=True, verbose_name="Активен")
    created_at = models.DateTimeField(verbose_name="Создан")

    class Meta:
        managed = False
        db_table = 'referral_codes'
        verbose_name = "Реферальный код"
        verbose_name_plural = "Реферальные коды"

    def __str__(self):
        return f"{self.code} ({self.owner_name})"


class ReferralActivations(models.Model):
    """Активации реферальных кодов"""
    id = models.BigAutoField(primary_key=True)
    user_id = models.CharField(max_length=64, unique=True, verbose_name="TG ID пользователя")
    referral_code_id = models.BigIntegerField(db_index=True, verbose_name="ID реф. кода")  # FK -> referral_codes.id
    activated_at = models.DateTimeField(verbose_name="Активирован")
    raw_text = models.CharField(max_length=64, null=True, blank=True, verbose_name="Исходный текст")
    source_platform = models.CharField(max_length=16, null=True, blank=True, verbose_name="Платформа (telegram/max)")

    class Meta:
        managed = False
        db_table = 'referral_activations'
        verbose_name = "Активация"
        verbose_name_plural = "Активации"

    def __str__(self):
        return f"Активация #{self.id}"


class SystemSetting(models.Model):
    """Системная настройка, доступная директору"""
    VALUE_TYPES = [
        ('number', 'Число'),
        ('text', 'Текст'),
    ]

    name = models.CharField(max_length=100, primary_key=True, verbose_name="Ключ")
    value_type = models.CharField(max_length=10, choices=VALUE_TYPES, verbose_name="Тип")
    char_value = models.CharField(max_length=1000, null=True, blank=True, verbose_name="Текстовое значение")
    number_value = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, verbose_name="Числовое значение")
    description = models.CharField(max_length=255, null=True, blank=True, verbose_name="Описание")
    updated_at = models.DateTimeField(verbose_name="Обновлено")
    updated_by = models.ForeignKey(
        'Specialists',
        models.DO_NOTHING,
        db_column='updated_by',
        null=True,
        blank=True,
        verbose_name="Кем обновлено",
    )

    class Meta:
        managed = False
        db_table = 'system_settings'
        verbose_name = "Системная настройка"
        verbose_name_plural = "Системные настройки"

    def __str__(self):
        return self.name


# ==============================================================================
# ЦЕНООБРАЗОВАНИЕ
# ==============================================================================

class ExtProductOperator(models.Model):
    """Справочник операторов/продуктовых названий внешнего сервиса"""
    CATEGORIES = [
        ('ofd', 'ОФД'),
        ('service', 'Сервис'),
        ('services_for_orders', 'Сервисы по заказу'),
        ('edo', 'ЭДО'),
    ]

    id = models.AutoField(primary_key=True)
    category = models.CharField(max_length=32, choices=CATEGORIES, verbose_name="Категория")
    operator_id = models.IntegerField(verbose_name="ID оператора")
    name = models.CharField(max_length=255, verbose_name="Название")
    is_active = models.BooleanField(default=True, verbose_name="Активен")
    sort_order = models.IntegerField(default=0, verbose_name="Порядок")
    updated_at = models.DateTimeField(verbose_name="Обновлено")

    class Meta:
        managed = False
        db_table = 'ext_product_operators'
        verbose_name = "Оператор внешнего продукта"
        verbose_name_plural = "Операторы внешних продуктов"
        unique_together = [['category', 'operator_id']]

    def __str__(self):
        return self.name


class ExtProductType(models.Model):
    """Справочник типов/сроков внешнего сервиса"""
    id = models.AutoField(primary_key=True)
    type_id = models.IntegerField(unique=True, verbose_name="ID типа")
    name = models.CharField(max_length=255, verbose_name="Название")
    months_number = models.IntegerField(null=True, blank=True, verbose_name="Количество месяцев")
    is_active = models.BooleanField(default=True, verbose_name="Активен")
    sort_order = models.IntegerField(default=0, verbose_name="Порядок")
    updated_at = models.DateTimeField(verbose_name="Обновлено")

    class Meta:
        managed = False
        db_table = 'ext_product_types'
        verbose_name = "Тип внешнего продукта"
        verbose_name_plural = "Типы внешних продуктов"

    def __str__(self):
        return self.name


class ExtProduct(models.Model):
    """Внешний товар и его ценовая стратегия"""
    CATEGORIES = [
        ('ofd', 'ОФД'),
        ('service', 'Сервис'),
        ('services_for_orders', 'Сервисы по заказу'),
        ('edo', 'ЭДО'),
    ]

    id = models.AutoField(primary_key=True)
    category = models.CharField(max_length=32, choices=CATEGORIES, verbose_name="Категория")
    operator_id = models.IntegerField(verbose_name="ID оператора")
    type_id = models.IntegerField(verbose_name="ID типа")
    external_name = models.CharField(max_length=255, null=True, blank=True, verbose_name="Внешнее название")
    source_price = models.IntegerField(null=True, blank=True, verbose_name="Цена внешнего сервиса")
    price_fixed = models.IntegerField(null=True, blank=True, verbose_name="Фикс. цена")
    markup_pct = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, verbose_name="Наценка %")
    is_active = models.BooleanField(default=True, verbose_name="Активен")
    comment = models.CharField(max_length=255, null=True, blank=True, verbose_name="Комментарий")
    last_seen_at = models.DateTimeField(null=True, blank=True, verbose_name="Последняя синхронизация")
    updated_at = models.DateTimeField(verbose_name="Обновлено")

    class Meta:
        managed = False
        db_table = 'ext_products'
        verbose_name = "Внешний продукт"
        verbose_name_plural = "Продукты со ВсеОФД"
        unique_together = [['category', 'operator_id', 'type_id']]

    def __str__(self):
        return f"{self.category}:{self.operator_id}:{self.type_id}"


# ==============================================================================
# АНАЛИТИКА БОТА
# ==============================================================================

class BotEvents(models.Model):
    """События бота"""
    id = models.BigAutoField(primary_key=True)
    ts = models.DateTimeField(db_index=True, verbose_name="Время")
    user_id = models.BigIntegerField(null=True, blank=True, db_index=True, verbose_name="ID пользователя")
    chat_id = models.BigIntegerField(null=True, blank=True, verbose_name="ID чата")
    message_id = models.BigIntegerField(null=True, blank=True, verbose_name="ID сообщения")
    update_id = models.BigIntegerField(null=True, blank=True, verbose_name="ID обновления")
    event_type = models.CharField(max_length=32, db_index=True, verbose_name="Тип события")
    name = models.CharField(max_length=128, null=True, blank=True, verbose_name="Название")
    callback_data = models.TextField(null=True, blank=True, verbose_name="Callback данные")
    text_len = models.IntegerField(null=True, blank=True, verbose_name="Длина текста")
    has_file = models.BooleanField(null=True, blank=True, verbose_name="Есть файл")
    payload = models.JSONField(null=True, blank=True, verbose_name="Payload")

    class Meta:
        managed = False
        db_table = 'bot_events'
        verbose_name = "Событие"
        verbose_name_plural = "События бота"

    def __str__(self):
        return f"{self.event_type}: {self.name or ''}"


class UiState(models.Model):
    """UI состояние"""
    tg_user_id = models.BigIntegerField(primary_key=True, verbose_name="TG ID")
    main_menu_mid = models.BigIntegerField(null=True, blank=True, verbose_name="ID главного меню")
    boot_notice_mid = models.BigIntegerField(null=True, blank=True, verbose_name="ID уведомления")
    updated_at = models.DateTimeField(verbose_name="Обновлено")

    class Meta:
        managed = False
        db_table = 'ui_state'
        verbose_name = "UI состояние"
        verbose_name_plural = "UI состояния"

    def __str__(self):
        return f"UI State: {self.tg_user_id}"


# ==============================================================================
# КАСТОМНЫЕ ПОЛЯ КЛИЕНТОВ (только для админов, не влияют на бота)
# ==============================================================================

class UserCustomFields(models.Model):
    """Кастомные поля клиентов - только для CRM, не отображаются в боте"""
    id = models.AutoField(primary_key=True)
    user_id = models.IntegerField(db_index=True, verbose_name="ID пользователя")
    field_name = models.CharField(max_length=100, verbose_name="Название поля")
    field_value = models.TextField(null=True, blank=True, verbose_name="Значение")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создано")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлено")

    class Meta:
        managed = False
        db_table = 'user_custom_fields'
        verbose_name = "Кастомное поле"
        verbose_name_plural = "Кастомные поля"
        unique_together = [['user_id', 'field_name']]

    def __str__(self):
        return f"{self.field_name}: {self.field_value[:50] if self.field_value else ''}"


# ==============================================================================
# LEGACY
# ==============================================================================

class OfdSubscriptions(models.Model):
    """ОФД подписки (legacy)"""
    id = models.AutoField(primary_key=True)
    client_id = models.IntegerField(db_index=True, verbose_name="ID клиента")
    kkt_slot = models.CharField(max_length=50, default='', verbose_name="Слот ККТ")
    ofd_expires_at = models.DateField(verbose_name="Дата окончания ОФД")
    source = models.CharField(max_length=255, null=True, blank=True, verbose_name="Источник")
    created_at = models.DateTimeField(null=True, blank=True, verbose_name="Создано")
    updated_at = models.DateTimeField(null=True, blank=True, verbose_name="Обновлено")

    class Meta:
        managed = False
        db_table = 'ofd_subscriptions'
        verbose_name = "ОФД подписка (legacy)"
        verbose_name_plural = "ОФД подписки (legacy)"
        unique_together = [['client_id', 'kkt_slot']]

    def __str__(self):
        return f"OFD Sub #{self.id}"


# ==============================================================================
# ЛОЯЛЬНОСТЬ (клиентский контур)
# ==============================================================================

class LoyaltyAccounts(models.Model):
    """Бонусный счёт клиента или партнёра"""
    id = models.BigAutoField(primary_key=True)
    owner_type = models.CharField(max_length=10, verbose_name="Тип владельца")
    owner_id = models.BigIntegerField(verbose_name="ID владельца")
    is_active = models.BooleanField(default=True, verbose_name="Активен")
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'loyalty_accounts'
        verbose_name = "Бонусный счёт"
        verbose_name_plural = "Бонусные счета"
        unique_together = [['owner_type', 'owner_id']]

    def __str__(self):
        return f"Account #{self.id} ({self.owner_type}:{self.owner_id})"


class LoyaltyTransactions(models.Model):
    """Журнал бонусных операций (ledger)"""
    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey(LoyaltyAccounts, on_delete=models.PROTECT, db_column='account_id')
    direction = models.CharField(max_length=6, verbose_name="Направление")
    amount = models.IntegerField(verbose_name="Сумма")
    event_type = models.CharField(max_length=64, verbose_name="Тип события")
    idempotency_key = models.CharField(max_length=128, unique=True, verbose_name="Ключ идемпотентности")
    description = models.CharField(max_length=255, null=True, blank=True, verbose_name="Описание")
    rating_delta = models.IntegerField(default=0, verbose_name="Дельта рейтинга")
    reversal_of_transaction = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.PROTECT,
        db_column='reversal_of_transaction_id', verbose_name="Сторно транзакции"
    )
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name="Сгорает")
    is_expired = models.BooleanField(default=False, verbose_name="Сгорел")
    ref_entity_type = models.CharField(max_length=32, null=True, blank=True, verbose_name="Тип связи")
    ref_entity_id = models.BigIntegerField(null=True, blank=True, verbose_name="ID связи")
    meta_json = models.JSONField(null=True, blank=True, verbose_name="Мета")
    created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'loyalty_transactions'
        verbose_name = "Бонусная транзакция"
        verbose_name_plural = "Бонусные транзакции"

    def __str__(self):
        return f"TX #{self.id} {self.direction} {self.amount} ({self.event_type})"


class LoyaltyBalances(models.Model):
    """Материализованный баланс бонусного счёта"""
    account = models.OneToOneField(LoyaltyAccounts, primary_key=True, on_delete=models.PROTECT, db_column='account_id')
    available_amount = models.IntegerField(default=0, verbose_name="Доступно")
    reserved_amount = models.IntegerField(default=0, verbose_name="Зарезервировано")
    total_earned = models.IntegerField(default=0, verbose_name="Всего начислено")
    total_spent = models.IntegerField(default=0, verbose_name="Всего списано")
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'loyalty_balances'
        verbose_name = "Баланс бонусов"
        verbose_name_plural = "Балансы бонусов"

    def __str__(self):
        return f"Balance {self.account_id}: {self.available_amount} avail"


class UserLoyaltyProfile(models.Model):
    """Профиль лояльности пользователя"""
    user_id = models.BigIntegerField(primary_key=True, verbose_name="ID пользователя")
    rating = models.IntegerField(default=0, verbose_name="Рейтинг")
    tier = models.CharField(max_length=32, default='none', verbose_name="Уровень")
    total_orders = models.IntegerField(default=0, verbose_name="Всего заказов")
    total_spent_rub = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Всего потрачено (руб)")
    last_activity_at = models.DateTimeField(null=True, blank=True, verbose_name="Последняя активность")
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'user_loyalty_profile'
        verbose_name = "Профиль лояльности"
        verbose_name_plural = "Профили лояльности"

    def __str__(self):
        return f"Loyalty user={self.user_id} tier={self.tier} rating={self.rating}"


# ==============================================================================
# ПАРТНЁРСКАЯ КОМПЕНСАЦИЯ
# ==============================================================================

class PartnerFinancialProfile(models.Model):
    """Финансовый профиль партнёра"""
    specialist = models.OneToOneField('Specialists', primary_key=True, on_delete=models.PROTECT, db_column='specialist_id')
    base_revenue_pct = models.DecimalField(max_digits=5, decimal_places=2, default=50, verbose_name="Базовая ставка %")
    override_revenue_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True, verbose_name="Override ставка %")
    rating = models.IntegerField(default=100, verbose_name="Рейтинг")
    rating_tier = models.CharField(max_length=32, default='standard', verbose_name="Уровень рейтинга")
    is_active = models.BooleanField(default=True, verbose_name="Активен")
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'partner_financial_profile'
        verbose_name = "Фин. профиль партнёра"
        verbose_name_plural = "Фин. профили партнёров"

    def __str__(self):
        return f"Partner {self.specialist_id}: {self.base_revenue_pct}%"


class PartnerRatingEvents(models.Model):
    """История изменений рейтинга партнёра"""
    id = models.BigAutoField(primary_key=True)
    specialist = models.ForeignKey('Specialists', on_delete=models.PROTECT, db_column='specialist_id')
    delta = models.IntegerField(verbose_name="Дельта")
    reason = models.CharField(max_length=64, verbose_name="Причина")
    comment = models.CharField(max_length=500, null=True, blank=True, verbose_name="Комментарий")
    initiated_by = models.ForeignKey(
        'Specialists', null=True, blank=True, on_delete=models.SET_NULL,
        db_column='initiated_by', related_name='initiated_rating_events', verbose_name="Инициатор"
    )
    rating_before = models.IntegerField(verbose_name="Рейтинг до")
    rating_after = models.IntegerField(verbose_name="Рейтинг после")
    ref_entity_type = models.CharField(max_length=32, null=True, blank=True)
    ref_entity_id = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'partner_rating_events'
        verbose_name = "Событие рейтинга партнёра"
        verbose_name_plural = "События рейтинга партнёров"

    def __str__(self):
        return f"Rating #{self.id}: {self.delta:+d} ({self.reason})"


class PartnerSalaryPeriods(models.Model):
    """Месячные расчёты зарплаты партнёра"""
    id = models.BigAutoField(primary_key=True)
    specialist = models.ForeignKey('Specialists', on_delete=models.PROTECT, db_column='specialist_id')
    period_month = models.CharField(max_length=7, verbose_name="Период (YYYY-MM)")
    status = models.CharField(max_length=10, default='draft', verbose_name="Статус")
    gross_revenue_rub = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Выручка")
    effective_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0, verbose_name="Итоговый %")
    base_salary_rub = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Базовая ЗП")
    adjustments_rub = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Корректировки")
    final_salary_rub = models.DecimalField(max_digits=12, decimal_places=2, default=0, verbose_name="Итого ЗП")
    approved_by = models.BigIntegerField(null=True, blank=True, verbose_name="Утвердил")
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата утверждения")
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата выплаты")
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'partner_salary_periods'
        verbose_name = "Зарплатный период"
        verbose_name_plural = "Зарплатные периоды"
        unique_together = [['specialist', 'period_month']]

    def __str__(self):
        return f"Salary {self.specialist_id} {self.period_month}: {self.final_salary_rub} руб"


class PartnerSalaryAdjustments(models.Model):
    """Ручные корректировки зарплаты"""
    id = models.BigAutoField(primary_key=True)
    salary_period = models.ForeignKey(PartnerSalaryPeriods, on_delete=models.PROTECT, db_column='salary_period_id')
    type = models.CharField(max_length=12, verbose_name="Тип")
    amount_rub = models.DecimalField(max_digits=12, decimal_places=2, verbose_name="Сумма")
    comment = models.CharField(max_length=500, verbose_name="Комментарий")
    created_by = models.ForeignKey('Specialists', on_delete=models.PROTECT, db_column='created_by')
    created_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'partner_salary_adjustments'
        verbose_name = "Корректировка ЗП"
        verbose_name_plural = "Корректировки ЗП"

    def __str__(self):
        return f"Adj #{self.id}: {self.amount_rub} ({self.type})"


# ==============================================================================
# НОРМАТИВНЫЕ СПРАВОЧНИКИ
# ==============================================================================

class BonusProductPolicies(models.Model):
    """Правила начисления баллов по продуктам"""
    id = models.AutoField(primary_key=True)
    product_type = models.CharField(max_length=64, verbose_name="Тип продукта")
    event_type = models.CharField(max_length=64, verbose_name="Тип события")
    points_fixed = models.IntegerField(null=True, blank=True, verbose_name="Фикс. баллы")
    points_per_rub = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True, verbose_name="Баллов/руб")
    points_lifetime_days = models.IntegerField(null=True, blank=True, verbose_name="Срок жизни (дни)")
    min_order_rub = models.IntegerField(null=True, blank=True, verbose_name="Мин. сумма заказа")
    max_points = models.IntegerField(null=True, blank=True, verbose_name="Макс. баллов")
    is_active = models.BooleanField(default=True, verbose_name="Активна")
    priority = models.IntegerField(default=0, verbose_name="Приоритет")
    valid_from = models.DateTimeField(null=True, blank=True, verbose_name="Действует с")
    valid_until = models.DateTimeField(null=True, blank=True, verbose_name="Действует до")
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'bonus_product_policies'
        verbose_name = "Политика начисления"
        verbose_name_plural = "Политики начисления"

    def __str__(self):
        return f"Policy #{self.id}: {self.product_type}/{self.event_type}"


class RatingTiers(models.Model):
    """Пороговые уровни рейтинга"""
    id = models.AutoField(primary_key=True)
    target_type = models.CharField(max_length=10, verbose_name="Тип")
    tier_name = models.CharField(max_length=32, verbose_name="Название уровня")
    min_rating = models.IntegerField(verbose_name="Мин. рейтинг")
    bonus_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0, verbose_name="Бонус %")
    description = models.CharField(max_length=255, null=True, blank=True, verbose_name="Описание")
    is_active = models.BooleanField(default=True, verbose_name="Активен")
    created_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'rating_tiers'
        verbose_name = "Уровень рейтинга"
        verbose_name_plural = "Уровни рейтинга"
        unique_together = [['target_type', 'tier_name']]

    def __str__(self):
        return f"{self.target_type}/{self.tier_name} (>={self.min_rating})"


# ==============================================================================
# ЗАДАЧИ
# ==============================================================================

class Tasks(models.Model):
    """Задачи CRM"""
    SOURCE_TYPES = [
        ('deal', 'Сделка'),
        ('renewal', 'Продление'),
        ('order', 'Заказ'),
        ('manual', 'Вручную'),
    ]
    PRIORITIES = [
        ('low', 'Низкий'),
        ('medium', 'Средний'),
        ('high', 'Высокий'),
    ]
    STATUSES = [
        ('new', 'Новая'),
        ('in_progress', 'В работе'),
        ('waiting', 'Ожидает'),
        ('done', 'Выполнена'),
    ]

    id = models.BigAutoField(primary_key=True)
    title = models.CharField(max_length=500, verbose_name="Заголовок")
    description = models.TextField(null=True, blank=True, verbose_name="Описание")
    source_type = models.CharField(max_length=32, choices=SOURCE_TYPES, default='manual', verbose_name="Источник")
    source_id = models.BigIntegerField(null=True, blank=True, verbose_name="ID источника")
    created_by = models.BigIntegerField(verbose_name="Создал (ID специалиста)")
    assignee_id = models.BigIntegerField(null=True, blank=True, verbose_name="Исполнитель (ID специалиста)")
    deadline = models.DateTimeField(null=True, blank=True, verbose_name="Срок выполнения")
    priority = models.CharField(max_length=16, choices=PRIORITIES, default='medium', verbose_name="Приоритет")
    status = models.CharField(max_length=32, choices=STATUSES, default='new', verbose_name="Статус")
    result_comment = models.TextField(null=True, blank=True, verbose_name="Комментарий/результат")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создана")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Обновлена")
    clients = models.ManyToManyField(
        Users,
        through='TaskClients',
        related_name='tasks',
        verbose_name="Клиенты по задаче",
    )

    class Meta:
        managed = False
        db_table = 'tasks'
        verbose_name = "Задача"
        verbose_name_plural = "Задачи"

    def __str__(self):
        return f"Задача #{self.id}: {self.title[:50]}"


class TaskClients(models.Model):
    """Связь задач и клиентов (M2M)."""
    id = models.BigAutoField(primary_key=True)
    task = models.ForeignKey(Tasks, on_delete=models.CASCADE, db_column='task_id', related_name='task_clients')
    client = models.ForeignKey(Users, on_delete=models.CASCADE, db_column='client_id', related_name='client_tasks')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Создана")

    class Meta:
        managed = False
        db_table = 'task_clients'
        verbose_name = "Клиент задачи"
        verbose_name_plural = "Клиенты задач"
        unique_together = [['task', 'client']]
