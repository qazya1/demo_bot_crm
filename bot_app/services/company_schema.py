"""Company and client schema helpers extracted from legacy views."""

from django.db import connection

from ..sql_cursor import with_sql_rewrite

connection = with_sql_rewrite(connection)


# ==================== CRM: АВТО-СОЗДАНИЕ КОМПАНИИ ====================

def _auto_create_or_link_company(client_id, cursor=None):
    """
    Если у контакта заполнено org_name — автоматически создаёт/находит компанию
    и привязывает контакт через company_contacts.
    """
    def _do(c):
        # Получаем данные контакта
        c.execute("""
            SELECT org_name, inn, kpp, phone, email, city
            FROM users WHERE id = %s
        """, [client_id])
        row = c.fetchone()
        if not row:
            return
        org_name, inn, kpp, phone, email, city = row

        if not org_name or not org_name.strip():
            return

        org_name = org_name.strip()
        company_id = None

        # 1) Ищем компанию по ИНН (точное совпадение, если есть)
        if inn and inn.strip():
            c.execute("""
                SELECT id FROM companies WHERE inn = %s LIMIT 1
            """, [inn.strip()])
            match = c.fetchone()
            if match:
                company_id = match[0]

        # 2) Если не нашли по ИНН — ищем по названию (точное)
        if company_id is None:
            c.execute("""
                SELECT id FROM companies WHERE name = %s LIMIT 1
            """, [org_name])
            match = c.fetchone()
            if match:
                company_id = match[0]

        # 3) Если компания не найдена — создаём новую
        if company_id is None:
            c.execute("""
                INSERT INTO companies (name, inn, kpp, phone, email, address)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, [
                org_name,
                inn.strip() if inn else None,
                kpp.strip() if kpp else None,
                phone.strip() if phone else None,
                email.strip() if email else None,
                city.strip() if city else None,
            ])
            c.execute("SELECT LAST_INSERT_ID()")
            company_id = c.fetchone()[0]
        else:
            # Обновляем пустые поля компании данными из контакта
            updates = []
            params = []
            if inn and inn.strip():
                updates.append("inn = COALESCE(inn, %s)")
                params.append(inn.strip())
            if kpp and kpp.strip():
                updates.append("kpp = COALESCE(kpp, %s)")
                params.append(kpp.strip())
            if phone and phone.strip():
                updates.append("phone = COALESCE(phone, %s)")
                params.append(phone.strip())
            if email and email.strip():
                updates.append("email = COALESCE(email, %s)")
                params.append(email.strip())
            if city and city.strip():
                updates.append("address = COALESCE(address, %s)")
                params.append(city.strip())
            if updates:
                params.append(company_id)
                c.execute(f"""
                    UPDATE companies SET {', '.join(updates)}, updated_at = NOW()
                    WHERE id = %s
                """, params)

        # 4) Привязываем контакт к компании (если ещё не привязан)
        c.execute("""
            INSERT IGNORE INTO company_contacts (company_id, contact_id)
            VALUES (%s, %s)
        """, [company_id, client_id])

    _ensure_companies_tables()
    if cursor:
        _do(cursor)
    else:
        with connection.cursor() as own_cursor:
            _do(own_cursor)


# ==================== CRM: КОМПАНИИ ====================

def _ensure_companies_tables():
    """Создаёт таблицы companies и company_contacts если не существуют"""
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                inn VARCHAR(20) DEFAULT NULL,
                kpp VARCHAR(20) DEFAULT NULL,
                ogrn VARCHAR(20) DEFAULT NULL,
                address TEXT DEFAULT NULL,
                phone VARCHAR(50) DEFAULT NULL,
                email VARCHAR(255) DEFAULT NULL,
                website VARCHAR(255) DEFAULT NULL,
                description TEXT DEFAULT NULL,
                note TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                KEY idx_inn (inn),
                KEY idx_name (name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS company_contacts (
                id INT AUTO_INCREMENT PRIMARY KEY,
                company_id INT NOT NULL,
                contact_id INT NOT NULL,
                role VARCHAR(100) DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                KEY idx_company (company_id),
                KEY idx_contact (contact_id),
                UNIQUE KEY unique_company_contact (company_id, contact_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        _ensure_client_kkt_company_column(cursor)
        _ensure_added_by_columns(cursor)
        try:
            cursor.execute("ALTER TABLE companies ADD COLUMN note TEXT DEFAULT NULL AFTER description")
        except Exception:
            pass


def _ensure_added_by_columns(cursor=None):
    """Добавляет служебные колонки связей специалистов/партнёров."""
    if cursor is None:
        with connection.cursor() as own_cursor:
            _ensure_added_by_columns(own_cursor)
        return

    # Добавляем added_by_specialist_id в users
    try:
        cursor.execute("""
            ALTER TABLE users ADD COLUMN added_by_specialist_id INT DEFAULT NULL
        """)
    except Exception:
        pass  # Колонка уже существует

    for sql in (
        "ALTER TABLE users ADD COLUMN added_by_partner_id BIGINT NULL",
        "ALTER TABLE users ADD COLUMN source_partner_id BIGINT NULL",
        "ALTER TABLE users ADD COLUMN partner_source_type VARCHAR(32) NULL",
        "CREATE INDEX ix_users_added_by_partner ON users(added_by_partner_id)",
        "CREATE INDEX ix_users_source_partner ON users(source_partner_id)",
        "CREATE INDEX ix_users_partner_source_type ON users(partner_source_type)",
    ):
        try:
            cursor.execute(sql)
        except Exception:
            pass

    # Добавляем added_by_specialist_id в companies
    try:
        cursor.execute("""
            ALTER TABLE companies ADD COLUMN added_by_specialist_id INT DEFAULT NULL
        """)
    except Exception:
        pass  # Колонка уже существует

    for sql in (
        "ALTER TABLE companies ADD COLUMN added_by_partner_id BIGINT NULL",
        "ALTER TABLE companies ADD COLUMN partner_source_type VARCHAR(32) NULL",
        "CREATE INDEX ix_companies_added_by_partner ON companies(added_by_partner_id)",
        "CREATE INDEX ix_companies_partner_source_type ON companies(partner_source_type)",
    ):
        try:
            cursor.execute(sql)
        except Exception:
            pass


def _ensure_users_birth_date_column(cursor=None):
    """Добавляет колонку birth_date в users, если её ещё нет."""
    if cursor is None:
        with connection.cursor() as own_cursor:
            _ensure_users_birth_date_column(own_cursor)
        return

    try:
        cursor.execute("""
            ALTER TABLE users
            ADD COLUMN birth_date DATE DEFAULT NULL AFTER city
        """)
    except Exception:
        pass  # Колонка уже существует


def _ensure_client_kkt_company_column(cursor=None):
    """Гарантирует наличие company_id в client_kkt для обратной совместимости БД."""
    if cursor is None:
        with connection.cursor() as own_cursor:
            _ensure_client_kkt_company_column(own_cursor)
        return

    # Добавляем company_id в client_kkt если ещё нет
    try:
        cursor.execute("""
            ALTER TABLE client_kkt ADD COLUMN company_id INT DEFAULT NULL AFTER client_id
        """)
    except Exception:
        pass  # Колонка уже существует

    # Разрешаем client_id быть NULL (для ККТ компаний)
    try:
        cursor.execute("""
            ALTER TABLE client_kkt MODIFY COLUMN client_id INT DEFAULT NULL
        """)
    except Exception:
        pass
