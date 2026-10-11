# -*- coding: utf-8 -*-
"""
Запуск тестов модуля FINANCE_CRM_TECH_PLAN
"""
import asyncio
import sys

# Цвета
G = '\033[92m'  # Green
R = '\033[91m'  # Red
Y = '\033[93m'  # Yellow
B = '\033[94m'  # Blue
X = '\033[0m'   # Reset

results = []

def ok(msg):
    print(f"{G}✓{X} {msg}")
    results.append(("PASS", msg))

def fail(msg, err=None):
    print(f"{R}✗{X} {msg}" + (f": {err}" if err else ""))
    results.append(("FAIL", msg, err))

def warn(msg):
    print(f"{Y}⚠{X} {msg}")
    results.append(("WARN", msg))

def section(title):
    print(f"\n{B}{'='*60}{X}")
    print(f"{B}{title}{X}")
    print(f"{B}{'='*60}{X}\n")


async def test_stage1_loyalty_enabled():
    """Feature flag LOYALTY_ENABLED"""
    try:
        from services.loyalty_engine import is_loyalty_enabled
        enabled = await is_loyalty_enabled()
        if isinstance(enabled, bool):
            ok(f"Feature flag работает (LOYALTY_ENABLED={enabled})")
            if not enabled:
                warn("LOYALTY_ENABLED=0 — система выключена")
        else:
            fail("is_loyalty_enabled() вернул не bool", enabled)
    except Exception as e:
        fail("Ошибка feature flag", e)


async def test_stage1_account_creation():
    """Создание loyalty account"""
    try:
        from services.ledger import ensure_account
        from db.loyalty import get_balance
        
        test_user_id = 999999
        account_id = await ensure_account("client", test_user_id)
        
        if account_id and account_id > 0:
            ok(f"Loyalty account создан (id={account_id})")
            balance = await get_balance(account_id)
            if balance:
                ok(f"Баланс создан (available={balance.get('available_amount', 0)})")
            else:
                fail("Баланс не создан")
        else:
            fail("Не удалось создать account")
    except Exception as e:
        fail("Ошибка создания account", e)


async def test_stage1_points_accrual():
    """Начисление баллов"""
    try:
        from services.ledger import post_accrual, get_available_balance
        from datetime import datetime
        
        test_user_id = 999999
        key = f"test_accrual_{datetime.utcnow().timestamp()}"
        
        result = await post_accrual(
            owner_type="client",
            owner_id=test_user_id,
            amount=100,
            event_type="test_accrual",
            idempotency_key=key,
            description="Тест",
            lifetime_days=365,
        )
        
        if result.get("ok") and not result.get("duplicate"):
            ok("Начисление баллов работает")
            balance = await get_available_balance("client", test_user_id)
            if balance >= 100:
                ok(f"Баланс обновлён (available={balance})")
            else:
                fail(f"Баланс не обновился (got {balance})")
        elif result.get("duplicate"):
            warn("Duplicate key — нормально для повторного запуска")
        else:
            fail("Начисление не удалось", result)
    except Exception as e:
        fail("Ошибка начисления", e)


async def test_stage1_order_integration():
    """Интеграция с confirm_order_paid"""
    try:
        from db.orders import confirm_order_paid
        import inspect
        import db.orders as orders_module
        
        sig = inspect.signature(confirm_order_paid)
        params = list(sig.parameters.keys())
        
        if "order_id" in params and "user_id" in params:
            ok("confirm_order_paid имеет правильную сигнатуру")
            
            source = inspect.getsource(orders_module)
            if "_award_loyalty_for_order" in source:
                ok("confirm_order_paid интегрирован с loyalty")
            else:
                fail("confirm_order_paid НЕ интегрирован (_award_loyalty_for_order не найден)")
        else:
            fail("confirm_order_paid неправильная сигнатура", params)
    except Exception as e:
        fail("Ошибка проверки интеграции orders", e)


async def test_stage1_profile_bonus_integration():
    """Интеграция бонусов за профиль"""
    try:
        import inspect
        
        try:
            import bot.bot_registration as bot_reg
            source = inspect.getsource(bot_reg)
            if "award_profile_completion_bonus" in source:
                ok("bot_registration.py интегрирован")
            else:
                fail("bot_registration.py НЕ интегрирован")
        except Exception as e:
            warn(f"Не удалось проверить bot_registration.py: {e}")
        
        try:
            import bot.max_registration as max_reg
            source = inspect.getsource(max_reg)
            if "award_profile_completion_bonus" in source:
                ok("max_registration.py интегрирован")
            else:
                fail("max_registration.py НЕ интегрирован")
        except Exception as e:
            warn(f"Не удалось проверить max_registration.py: {e}")
    except Exception as e:
        fail("Ошибка проверки бонусов за профиль", e)


async def test_stage2_dashboard_exists():
    """Страница финансового дашборда"""
    try:
        from bot_app import views_loyalty
        
        if hasattr(views_loyalty, 'financial_dashboard_page'):
            ok("financial_dashboard_page существует")
        else:
            fail("financial_dashboard_page НЕ найдена")
    except Exception as e:
        fail("Ошибка проверки дашборда", e)


async def test_stage3_manual_operations_exists():
    """Страница ручных операций"""
    try:
        from bot_app import views_loyalty
        
        if hasattr(views_loyalty, 'manual_operations_page'):
            ok("manual_operations_page существует")
        else:
            fail("manual_operations_page НЕ найдена")
    except Exception as e:
        fail("Ошибка проверки ручных операций", e)


async def test_stage4_client_loyalty_summary():
    """Данные лояльности клиента"""
    try:
        from services.loyalty_engine import get_user_loyalty_summary
        
        test_user_id = 999999
        summary = await get_user_loyalty_summary(test_user_id)
        
        if summary:
            ok("get_user_loyalty_summary работает")
            required = ["balance", "rating", "tier", "total_orders", "history"]
            missing = [k for k in required if k not in summary]
            
            if not missing:
                ok("Все поля присутствуют в summary")
            else:
                fail(f"Отсутствуют поля: {missing}")
        else:
            fail("get_user_loyalty_summary вернул пустой результат")
    except Exception as e:
        fail("Ошибка получения данных клиента", e)


async def test_stage5_partner_profile():
    """Финансовый профиль партнёра"""
    try:
        from db.partner_finance import ensure_partner_financial_profile
        
        test_specialist_id = 999999
        profile = await ensure_partner_financial_profile(
            test_specialist_id,
            base_revenue_pct=50.0,
            initial_rating=100,
        )
        
        if profile:
            ok("Профиль партнёра создан")
            if profile.get("base_revenue_pct") == 50.0:
                ok("Базовая ставка корректна")
            else:
                fail(f"Ставка неверна: {profile.get('base_revenue_pct')}")
        else:
            fail("Не удалось создать профиль партнёра")
    except Exception as e:
        fail("Ошибка создания профиля партнёра", e)


async def test_stage5_salary_calculation():
    """Расчёт зарплаты партнёра"""
    try:
        from services.partner_compensation import calculate_partner_salary
        from datetime import datetime
        
        test_specialist_id = 999999
        period_month = datetime.utcnow().strftime("%Y-%m")
        
        result = await calculate_partner_salary(test_specialist_id, period_month)
        
        if result:
            ok("Расчёт зарплаты работает")
            required = ["gross_revenue_rub", "effective_pct", "base_salary_rub", "final_salary_rub"]
            missing = [k for k in required if k not in result]
            
            if not missing:
                ok("Все поля зарплаты присутствуют")
            else:
                fail(f"Отсутствуют поля: {missing}")
        else:
            fail("Расчёт зарплаты вернул пустой результат")
    except Exception as e:
        fail("Ошибка расчёта зарплаты", e)


async def run_all():
    section("ЭТАП 1: Начисление баллов")
    await test_stage1_loyalty_enabled()
    await test_stage1_account_creation()
    await test_stage1_points_accrual()
    await test_stage1_order_integration()
    await test_stage1_profile_bonus_integration()
    
    section("ЭТАП 2: Финансовый дашборд")
    await test_stage2_dashboard_exists()
    
    section("ЭТАП 3: Ручные операции")
    await test_stage3_manual_operations_exists()
    
    section("ЭТАП 4: Финблок в карточке клиента")
    await test_stage4_client_loyalty_summary()
    
    section("ЭТАП 5: Аналитика и партнёры")
    await test_stage5_partner_profile()
    await test_stage5_salary_calculation()


def print_summary():
    section("ИТОГОВЫЙ ОТЧЁТ")
    
    passed = sum(1 for r in results if r[0] == "PASS")
    failed = sum(1 for r in results if r[0] == "FAIL")
    warned = sum(1 for r in results if r[0] == "WARN")
    
    print(f"\n{G}✓ Пройдено:{X} {passed}")
    print(f"{R}✗ Провалено:{X} {failed}")
    print(f"{Y}⚠ Предупреждений:{X} {warned}")
    print(f"\nВсего тестов: {len(results)}\n")
    
    if failed > 0:
        print(f"{R}{'='*60}{X}")
        print(f"{R}ПРОВАЛЕННЫЕ ТЕСТЫ:{X}\n")
        for r in results:
            if r[0] == "FAIL":
                msg = r[1]
                error = r[2] if len(r) > 2 else None
                print(f"{R}✗{X} {msg}")
                if error:
                    print(f"  Ошибка: {error}\n")
    
    if warned > 0:
        print(f"{Y}{'='*60}{X}")
        print(f"{Y}ПРЕДУПРЕЖДЕНИЯ:{X}\n")
        for r in results:
            if r[0] == "WARN":
                print(f"{Y}⚠{X} {r[1]}")
    
    return failed == 0


if __name__ == "__main__":
    print(f"\n{B}{'='*60}{X}")
    print(f"{B}ТЕСТИРОВАНИЕ МОДУЛЯ FINANCE_CRM_TECH_PLAN{X}")
    print(f"{B}{'='*60}{X}\n")
    
    try:
        asyncio.run(run_all())
        success = print_summary()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print(f"\n{Y}Прервано пользователем{X}")
        sys.exit(130)
    except Exception as e:
        print(f"\n{R}Критическая ошибка: {e}{X}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
