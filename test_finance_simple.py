# -*- coding: utf-8 -*-
"""
Простой тест модуля FINANCE_CRM_TECH_PLAN (без Unicode символов для Windows)
"""
import asyncio
import sys

results = []

def ok(msg):
    print(f"[OK] {msg}")
    results.append(("PASS", msg))

def fail(msg, err=None):
    print(f"[FAIL] {msg}" + (f": {err}" if err else ""))
    results.append(("FAIL", msg, err))

def warn(msg):
    print(f"[WARN] {msg}")
    results.append(("WARN", msg))

def section(title):
    print(f"\n{'='*60}")
    print(f"{title}")
    print(f"{'='*60}\n")


async def test_stage1_loyalty_enabled():
    """Feature flag LOYALTY_ENABLED"""
    try:
        from services.loyalty_engine import is_loyalty_enabled
        enabled = await is_loyalty_enabled()
        if isinstance(enabled, bool):
            ok(f"Feature flag rabotaet (LOYALTY_ENABLED={enabled})")
            if not enabled:
                warn("LOYALTY_ENABLED=0 - sistema vykluchena")
        else:
            fail("is_loyalty_enabled() vernul ne bool", enabled)
    except Exception as e:
        fail("Oshibka feature flag", str(e))


async def test_stage1_account_creation():
    """Sozdanie loyalty account"""
    try:
        from services.ledger import ensure_account
        from db.loyalty import get_balance
        
        test_user_id = 999999
        account_id = await ensure_account("client", test_user_id)
        
        if account_id and account_id > 0:
            ok(f"Loyalty account sozdan (id={account_id})")
            balance = await get_balance(account_id)
            if balance:
                ok(f"Balans sozdan (available={balance.get('available_amount', 0)})")
            else:
                fail("Balans ne sozdan")
        else:
            fail("Ne udalos sozdat account")
    except Exception as e:
        fail("Oshibka sozdaniya account", str(e))


async def test_stage1_points_accrual():
    """Nachislenie ballov"""
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
            description="Test",
            lifetime_days=365,
        )
        
        if result.get("ok") and not result.get("duplicate"):
            ok("Nachislenie ballov rabotaet")
            balance = await get_available_balance("client", test_user_id)
            if balance >= 100:
                ok(f"Balans obnovlen (available={balance})")
            else:
                fail(f"Balans ne obnovilsya (got {balance})")
        elif result.get("duplicate"):
            warn("Duplicate key - normalno dlya povtornogo zapuska")
        else:
            fail("Nachislenie ne udalos", str(result))
    except Exception as e:
        fail("Oshibka nachisleniya", str(e))


async def test_stage1_order_integration():
    """Integratsiya s confirm_order_paid"""
    try:
        from db.orders import confirm_order_paid
        import inspect
        import db.orders as orders_module
        
        sig = inspect.signature(confirm_order_paid)
        params = list(sig.parameters.keys())
        
        if "order_id" in params and "user_id" in params:
            ok("confirm_order_paid imeet pravilnuyu signaturu")
            
            source = inspect.getsource(orders_module)
            if "_award_loyalty_for_order" in source:
                ok("confirm_order_paid integrirovan s loyalty")
            else:
                fail("confirm_order_paid NE integrirovan (_award_loyalty_for_order ne nayden)")
        else:
            fail("confirm_order_paid nepravilnaya signatura", str(params))
    except Exception as e:
        fail("Oshibka proverki integratsii orders", str(e))


async def test_stage1_profile_bonus_integration():
    """Integratsiya bonusov za profil"""
    try:
        import inspect
        
        try:
            import bot.bot_registration as bot_reg
            source = inspect.getsource(bot_reg)
            if "award_profile_completion_bonus" in source:
                ok("bot_registration.py integrirovan")
            else:
                fail("bot_registration.py NE integrirovan")
        except Exception as e:
            warn(f"Ne udalos proverit bot_registration.py: {e}")
        
        try:
            import bot.max_registration as max_reg
            source = inspect.getsource(max_reg)
            if "award_profile_completion_bonus" in source:
                ok("max_registration.py integrirovan")
            else:
                fail("max_registration.py NE integrirovan")
        except Exception as e:
            warn(f"Ne udalos proverit max_registration.py: {e}")
    except Exception as e:
        fail("Oshibka proverki bonusov za profil", str(e))


async def test_stage2_dashboard_exists():
    """Stranitsa finansovogo dashborda"""
    try:
        from bot_app import views_loyalty
        
        if hasattr(views_loyalty, 'financial_dashboard_page'):
            ok("financial_dashboard_page suschestvuet")
        else:
            fail("financial_dashboard_page NE naydena")
    except Exception as e:
        fail("Oshibka proverki dashborda", str(e))


async def test_stage3_manual_operations_exists():
    """Stranitsa ruchnykh operatsiy"""
    try:
        from bot_app import views_loyalty
        
        if hasattr(views_loyalty, 'manual_operations_page'):
            ok("manual_operations_page suschestvuet")
        else:
            fail("manual_operations_page NE naydena")
    except Exception as e:
        fail("Oshibka proverki ruchnykh operatsiy", str(e))


async def test_stage4_client_loyalty_summary():
    """Dannye loyalnosti klienta"""
    try:
        from services.loyalty_engine import get_user_loyalty_summary
        
        test_user_id = 999999
        summary = await get_user_loyalty_summary(test_user_id)
        
        if summary:
            ok("get_user_loyalty_summary rabotaet")
            required = ["balance", "rating", "tier", "total_orders", "history"]
            missing = [k for k in required if k not in summary]
            
            if not missing:
                ok("Vse polya prisutstvuyut v summary")
            else:
                fail(f"Otsutstvuyut polya: {missing}")
        else:
            fail("get_user_loyalty_summary vernul pustoy rezultat")
    except Exception as e:
        fail("Oshibka polucheniya dannykh klienta", str(e))


async def test_stage5_partner_profile():
    """Finansovyy profil partnera"""
    try:
        from db.partner_finance import ensure_partner_financial_profile
        
        test_specialist_id = 999999
        profile = await ensure_partner_financial_profile(
            test_specialist_id,
            base_revenue_pct=50.0,
            initial_rating=100,
        )
        
        if profile:
            ok("Profil partnera sozdan")
            if profile.get("base_revenue_pct") == 50.0:
                ok("Bazovaya stavka korrektna")
            else:
                fail(f"Stavka neverna: {profile.get('base_revenue_pct')}")
        else:
            fail("Ne udalos sozdat profil partnera")
    except Exception as e:
        fail("Oshibka sozdaniya profilya partnera", str(e))


async def test_stage5_salary_calculation():
    """Raschet zarplaty partnera"""
    try:
        from services.partner_compensation import calculate_partner_salary
        from datetime import datetime
        
        test_specialist_id = 999999
        period_month = datetime.utcnow().strftime("%Y-%m")
        
        result = await calculate_partner_salary(test_specialist_id, period_month)
        
        if result:
            ok("Raschet zarplaty rabotaet")
            required = ["gross_revenue_rub", "effective_pct", "base_salary_rub", "final_salary_rub"]
            missing = [k for k in required if k not in result]
            
            if not missing:
                ok("Vse polya zarplaty prisutstvuyut")
            else:
                fail(f"Otsutstvuyut polya: {missing}")
        else:
            fail("Raschet zarplaty vernul pustoy rezultat")
    except Exception as e:
        fail("Oshibka rascheta zarplaty", str(e))


async def run_all():
    section("ETAP 1: Nachislenie ballov")
    await test_stage1_loyalty_enabled()
    await test_stage1_account_creation()
    await test_stage1_points_accrual()
    await test_stage1_order_integration()
    await test_stage1_profile_bonus_integration()
    
    section("ETAP 2: Finansovyy dashbord")
    await test_stage2_dashboard_exists()
    
    section("ETAP 3: Ruchnye operatsii")
    await test_stage3_manual_operations_exists()
    
    section("ETAP 4: Finblok v kartochke klienta")
    await test_stage4_client_loyalty_summary()
    
    section("ETAP 5: Analitika i partnery")
    await test_stage5_partner_profile()
    await test_stage5_salary_calculation()


def print_summary():
    section("ITOGOVYY OTCHET")
    
    passed = sum(1 for r in results if r[0] == "PASS")
    failed = sum(1 for r in results if r[0] == "FAIL")
    warned = sum(1 for r in results if r[0] == "WARN")
    
    print(f"\n[+] Proydeno: {passed}")
    print(f"[-] Provaleno: {failed}")
    print(f"[!] Preduprezhdeniy: {warned}")
    print(f"\nVsego testov: {len(results)}\n")
    
    if failed > 0:
        print(f"{'='*60}")
        print(f"PROVALENNYE TESTY:\n")
        for r in results:
            if r[0] == "FAIL":
                msg = r[1]
                error = r[2] if len(r) > 2 else None
                print(f"[-] {msg}")
                if error:
                    print(f"    Oshibka: {error}\n")
    
    if warned > 0:
        print(f"{'='*60}")
        print(f"PREDUPREZHDENIYA:\n")
        for r in results:
            if r[0] == "WARN":
                print(f"[!] {r[1]}")
    
    return failed == 0


if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"TESTIROVANIE MODULYA FINANCE_CRM_TECH_PLAN")
    print(f"{'='*60}\n")
    
    try:
        asyncio.run(run_all())
        success = print_summary()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print(f"\nPrervano polzovatelem")
        sys.exit(130)
    except Exception as e:
        print(f"\nKriticheskaya oshibka: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
