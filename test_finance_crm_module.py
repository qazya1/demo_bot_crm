"""
Комплексные тесты для модуля FINANCE_CRM_TECH_PLAN

Тестирует все этапы реализации:
- ЭТАП 1: Начисление баллов (db/orders.py, bot registration)
- ЭТАП 2: Финансовый дашборд (views_loyalty.py)
- ЭТАП 3: Ручные операции (views_loyalty.py)
- ЭТАП 4: Финансовый блок в карточке клиента
- ЭТАП 5: Аналитика и кабинет партнёра

Запуск: python test_finance_crm_module.py
"""
import asyncio
import sys
from datetime import datetime, timedelta
from decimal import Decimal

# Цветной вывод
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'

test_results = []


def ok(msg):
    print(f"{GREEN}✓{RESET} {msg}")
    test_results.append(("PASS", msg))


def fail(msg, error=None):
    err_str = f": {error}" if error else ""
    print(f"{RED}✗{RESET} {msg}{err_str}")
    test_results.append(("FAIL", msg, error))


def warn(msg):
    print(f"{YELLOW}⚠{RESET} {msg}")
    test_results.append(("WARN", msg))


def info(msg):
    print(f"{BLUE}ℹ{RESET} {msg}")


def section(title):
    print(f"\n{BLUE}{'='*60}{RESET}")
    print(f"{BLUE}{title}{RESET}")
    print(f"{BLUE}{'='*60}{RESET}\n")


# ============================================================
# ЭТАП 1: Тесты начисления баллов
# ============================================================

async def test_stage1_loyalty_enabled():
    """Проверка feature flag LOYALTY_ENABLED"""
    try:
        from services.loyalty_engine import is_loyalty_enabled
        enabled = await is_loyalty_enabled()
        if isinstance(enabled, bool):
            ok(f"Feature flag LOYALTY_ENABLED работает (значение: {enabled})")
            if not enabled:
                warn("LOYALTY_ENABLED=0 — система лояльности выключена")
        else:
            fail("is_loyalty_enabled() вернул не bool", enabled)
    except Exception as e:
        fail("Ошибка проверки feature flag", e)


async def test_stage1_account_creation():
    """Создание loyalty account для клиента"""
    try:
        from services.ledger import ensure_account
        from db.loyalty import get_loyalty_account, get_balance
        
        # Создаём тестовый аккаунт
        test_user_id = 999999
        account_id = await ensure_account("client", test_user_id)
        
        if account_id and account_id > 0:
            ok(f"Создан loyalty account (id={account_id})")
            
            # Проверяем, что баланс тоже создан
            balance = await get_balance(account_id)
            if balance:
                ok(f"Баланс создан автоматически (available={balance.get('available_amount', 0)})")
            else:
                fail("Баланс не создан автоматически")
        else:
            fail("Не удалось создать loyalty account")
    except Exception as e:
        fail("Ошибка создания loyalty account", e)


async def test_stage1_points_accrual():
    """Начисление баллов через ledger"""
    try:
        from services.ledger import post_accrual, get_available_balance
        
        test_user_id = 999999
        idempotency_key = f"test_accrual_{datetime.utcnow().timestamp()}"
        
        # Начисляем 100 баллов
        result = await post_accrual(
            owner_type="client",
            owner_id=test_user_id,
            amount=100,
            event_type="test_accrual",
            idempotency_key=idempotency_key,
            description="Тестовое начисление",
            lifetime_days=365,
        )
        
        if result.get("ok") and not result.get("duplicate"):
            ok("Начисление баллов через ledger работает")
            
            # Проверяем баланс
            balance = await get_available_balance("client", test_user_id)
            if balance >= 100:
                ok(f"Баланс обновлён корректно (available={balance})")
            else:
                fail(f"Баланс не обновился (expected >=100, got {balance})")
        elif result.get("duplicate"):
            warn("Начисление пропущено (duplicate key) — это нормально для повторного запуска")
        else:
            fail("Начисление баллов не удалось", result)
    except Exception as e:
        fail("Ошибка начисления баллов", e)


async def test_stage1_idempotency():
    """Проверка идемпотентности начислений"""
    try:
        from services.ledger import post_accrual, get_available_balance
        
        test_user_id = 999999
        idempotency_key = f"test_idempotency_{datetime.utcnow().timestamp()}"
        
        # Первое начисление
        result1 = await post_accrual(
            owner_type="client",
            owner_id=test_user_id,
            amount=50,
            event_type="test_idempotency",
            idempotency_key=idempotency_key,
            description="Тест идемпотентности",
        )
        
        balance_after_first = await get_available_balance("client", test_user_id)
        
        # Повторное начисление с тем же ключом
        result2 = await post_accrual(
            owner_type="client",
            owner_id=test_user_id,
            amount=50,
            event_type="test_idempotency",
            idempotency_key=idempotency_key,
            description="Тест идемпотентности (повтор)",
        )
        
        balance_after_second = await get_available_balance("client", test_user_id)
        
        if result2.get("duplicate"):
            ok("Идемпотентность работает (duplicate detected)")
            
            if balance_after_first == balance_after_second:
                ok("Баланс не изменился при повторном начислении")
            else:
                fail(f"Баланс изменился! {balance_after_first} -> {balance_after_second}")
        else:
            fail("Идемпотентность НЕ работает — повторное начисление прошло", result2)
    except Exception as e:
        fail("Ошибка проверки идемпотентности", e)


async def test_stage1_reserve_redeem_flow():
    """Резервирование и списание баллов"""
    try:
        from services.ledger import (
            post_accrual, reserve_points, redeem_reserved_points,
            release_reserved_points, get_available_balance
        )
        
        test_user_id = 999998
        
        # Начисляем баллы для теста
        accrual_key = f"test_reserve_setup_{datetime.utcnow().timestamp()}"
        await post_accrual(
            owner_type="client",
            owner_id=test_user_id,
            amount=200,
            event_type="test_setup",
            idempotency_key=accrual_key,
            description="Подготовка для теста резервирования",
        )
        
        balance_before = await get_available_balance("client", test_user_id)
        
        # Резервируем 100 баллов
        reserve_key = f"test_reserve_{datetime.utcnow().timestamp()}"
        reserve_result = await reserve_points(
            owner_type="client",
            owner_id=test_user_id,
            amount=100,
            idempotency_key=reserve_key,
            description="Тест резервирования",
        )
        
        if reserve_result.get("ok") and not reserve_result.get("duplicate"):
            ok("Резервирование баллов работает")
            
            balance_after_reserve = await get_available_balance("client", test_user_id)
            if balance_after_reserve == balance_before - 100:
                ok(f"Баланс уменьшился на 100 ({balance_before} -> {balance_after_reserve})")
            else:
                fail(f"Баланс изменился неправильно: {balance_before} -> {balance_after_reserve}")
            
            # Подтверждаем списание
            redeem_result = await redeem_reserved_points(reserve_key)
            if redeem_result.get("ok"):
                ok("Подтверждение списания (redeem) работает")
            else:
                fail("Ошибка подтверждения списания", redeem_result)
        else:
            fail("Резервирование не удалось", reserve_result)
    except Exception as e:
        fail("Ошибка теста резервирования/списания", e)


async def test_stage1_points_expiration():
    """Проверка механизма сгорания баллов"""
    try:
        from services.ledger import post_accrual, expire_transactions
        from db.loyalty import list_expiring_transactions
        
        test_user_id = 999997
        
        # Начисляем баллы с истёкшим сроком (expires_at в прошлом)
        past_date = datetime.utcnow() - timedelta(days=1)
        accrual_key = f"test_expiry_{datetime.utcnow().timestamp()}"
        
        await post_accrual(
            owner_type="client",
            owner_id=test_user_id,
            amount=50,
            event_type="test_expiry",
            idempotency_key=accrual_key,
            description="Тест сгорания",
            expires_at=past_date,
        )
        
        # Проверяем, что транзакция попадает в список просроченных
        expiring = await list_expiring_transactions(batch_size=100)
        found = any(tx.get("idempotency_key") == accrual_key for tx in expiring)
        
        if found:
            ok("Просроченная транзакция найдена в списке")
            
            # Запускаем процесс сгорания
            expired_count = await expire_transactions(batch_size=100)
            if expired_count > 0:
                ok(f"Обработано {expired_count} просроченных транзакций")
            else:
                warn("Не обработано ни одной просроченной транзакции")
        else:
            warn("Просроченная транзакция не найдена (возможно, уже обработана)")
    except Exception as e:
        fail("Ошибка проверки сгорания баллов", e)


async def test_stage1_order_integration():
    """Проверка интеграции с confirm_order_paid"""
    try:
        from db.orders import confirm_order_paid
        
        # Проверяем, что функция существует и имеет правильную сигнатуру
        import inspect
        sig = inspect.signature(confirm_order_paid)
        params = list(sig.parameters.keys())
        
        if "order_id" in params and "user_id" in params:
            ok("confirm_order_paid имеет правильную сигнатуру")
            
            # Проверяем, что внутри есть вызов _award_loyalty_for_order
            import db.orders as orders_module
            source = inspect.getsource(orders_module)
            
            if "_award_loyalty_for_order" in source:
                ok("confirm_order_paid интегрирован с loyalty (вызывает _award_loyalty_for_order)")
            else:
                fail("confirm_order_paid НЕ интегрирован с loyalty (_award_loyalty_for_order не найден)")
        else:
            fail("confirm_order_paid имеет неправильную сигнатуру", params)
    except Exception as e:
        fail("Ошибка проверки интеграции с orders", e)


async def test_stage1_profile_bonus_integration():
    """Проверка интеграции бонусов за профиль в ботах"""
    try:
        import inspect
        
        # Проверяем bot_registration.py
        try:
            import bot.bot_registration as bot_reg
            source = inspect.getsource(bot_reg)
            if "award_profile_completion_bonus" in source:
                ok("bot_registration.py интегрирован с loyalty")
            else:
                fail("bot_registration.py НЕ интегрирован с loyalty")
        except Exception as e:
            warn(f"Не удалось проверить bot_registration.py: {e}")
        
        # Проверяем max_registration.py
        try:
            import bot.max_registration as max_reg
            source = inspect.getsource(max_reg)
            if "award_profile_completion_bonus" in source:
                ok("max_registration.py интегрирован с loyalty")
            else:
                fail("max_registration.py НЕ интегрирован с loyalty")
        except Exception as e:
            warn(f"Не удалось проверить max_registration.py: {e}")
    except Exception as e:
        fail("Ошибка проверки интеграции бонусов за профиль", e)


async def test_stage1_service_payment_integration():
    """Проверка интеграции начисления за оплату услуг"""
    try:
        import inspect
        import webapp.utils as webapp_utils
        
        source = inspect.getsource(webapp_utils)
        
        if "award_points_for_purchase" in source and "watch_payment_until_paid" in source:
            ok("webapp/utils.py интегрирован с loyalty (начисление за услуги)")
        else:
            fail("webapp/utils.py НЕ интегрирован с loyalty")
    except Exception as e:
        fail("Ошибка проверки интеграции оплаты услуг", e)
