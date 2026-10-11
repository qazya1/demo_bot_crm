"""Authentication views and helpers for CRM."""

import hashlib
import logging
from datetime import datetime, timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.db import connection
from django.shortcuts import redirect, render

from ..sql_cursor import with_sql_rewrite

connection = with_sql_rewrite(connection)
logger = logging.getLogger(__name__)

# Макс. попыток до блокировки, и время блокировки в минутах
MAX_LOGIN_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15


def _old_hash_password(password):
    """Старое хэширование SHA256 + соль (только для миграции)"""
    salt = 'bot_crm_salt_2026'
    return hashlib.sha256((password + salt).encode()).hexdigest()


def hash_password(password):
    """Хэширование пароля через Django PBKDF2"""
    return make_password(password)


def verify_password(password, stored_hash):
    """Проверка пароля с поддержкой старых SHA256 хэшей.
    Возвращает (success: bool, needs_rehash: bool)."""
    # Новый формат Django (pbkdf2_sha256$...) — проверяем через Django
    if stored_hash and stored_hash.startswith(('pbkdf2_sha256$', 'bcrypt$', 'argon2')):
        return check_password(password, stored_hash), False
    # Старый формат SHA256 — проверяем напрямую и помечаем для перехэширования
    if stored_hash == _old_hash_password(password):
        return True, True
    return False, False


# ==================== ОГРАНИЧЕНИЕ ПОПЫТОК ВХОДА ====================


def _ensure_login_attempts_table():
    """Создаёт таблицу login_attempts если не существует"""
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INT AUTO_INCREMENT PRIMARY KEY,
                login VARCHAR(128) NOT NULL,
                ip_address VARCHAR(45) NOT NULL,
                attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                success TINYINT DEFAULT 0,
                KEY idx_login_ip (login, ip_address),
                KEY idx_attempted (attempted_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)


def _get_client_ip(request):
    """Получить IP клиента"""
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def _check_login_rate_limit(login_name, ip_address):
    """Проверяет, не превышен ли лимит попыток. Возвращает (blocked, minutes_left)."""
    with connection.cursor() as cursor:
        cutoff = datetime.now() - timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
        cursor.execute("""
            SELECT COUNT(*) FROM login_attempts
            WHERE login = %s AND ip_address = %s AND attempted_at > %s AND success = 0
        """, [login_name, ip_address, cutoff])
        failed_count = cursor.fetchone()[0]
        if failed_count >= MAX_LOGIN_ATTEMPTS:
            # Вычисляем сколько минут осталось до разблокировки
            cursor.execute("""
                SELECT attempted_at FROM login_attempts
                WHERE login = %s AND ip_address = %s AND success = 0
                ORDER BY attempted_at DESC LIMIT 1
            """, [login_name, ip_address])
            last_row = cursor.fetchone()
            if last_row:
                unlock_at = last_row[0] + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
                minutes_left = max(1, int((unlock_at - datetime.now()).total_seconds() / 60) + 1)
                return True, minutes_left
        return False, 0


def _record_login_attempt(login_name, ip_address, success):
    """Записывает попытку входа"""
    with connection.cursor() as cursor:
        cursor.execute("""
            INSERT INTO login_attempts (login, ip_address, success)
            VALUES (%s, %s, %s)
        """, [login_name, ip_address, 1 if success else 0])
        # При успешном входе сбрасываем неудачные попытки
        if success:
            cursor.execute("""
                DELETE FROM login_attempts
                WHERE login = %s AND ip_address = %s AND success = 0
            """, [login_name, ip_address])


def login_page(request):
    """Страница входа"""
    _ensure_login_attempts_table()
    error = None

    if request.method == 'POST':
        login = request.POST.get('login', '').strip()
        password = request.POST.get('password', '')
        ip = _get_client_ip(request)

        # Проверка rate limit
        blocked, minutes_left = _check_login_rate_limit(login, ip)
        if blocked:
            error = f'Слишком много попыток. Попробуйте через {minutes_left} мин.'
            return render(request, 'bot_app/login.html', {'error': error})

        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT id, name, password_hash, is_director, profession
                FROM specialists
                WHERE login = %s
            """, [login])
            row = cursor.fetchone()

            if row:
                specialist_id, name, password_hash, is_director, profession = row
                # Проверяем пароль (с поддержкой старого SHA256)
                ok, needs_rehash = verify_password(password, password_hash)
                if ok:
                    # Перехэшируем старый SHA256 → PBKDF2 автоматически
                    if needs_rehash:
                        new_hash = hash_password(password)
                        cursor.execute(
                            "UPDATE specialists SET password_hash = %s WHERE id = %s",
                            [new_hash, specialist_id]
                        )
                        logger.info("Password rehashed SHA256->PBKDF2 for specialist id=%s", specialist_id)

                    _record_login_attempt(login, ip, success=True)

                    # Успешный вход
                    request.session['specialist_id'] = specialist_id
                    request.session['specialist_name'] = name
                    request.session['is_director'] = bool(is_director)

                    if is_director:
                        return redirect('bot_app:dashboard')
                    if int(profession or 0) == 5:
                        return redirect('bot_app:partner_clients')
                    else:
                        return redirect('bot_app:specialist_panel')
                else:
                    _record_login_attempt(login, ip, success=False)
                    error = 'Неверный пароль'
            else:
                _record_login_attempt(login, ip, success=False)
                error = 'Пользователь не найден'

    return render(request, 'bot_app/login.html', {'error': error})


def logout_view(request):
    """Выход из системы"""
    request.session.flush()
    return redirect('bot_app:login')
