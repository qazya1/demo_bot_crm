# main.py
import logging
import time
import asyncio
import signal
from contextlib import asynccontextmanager
from collections import defaultdict
from typing import List
import re

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles

from config import setup_logging, BOT_TOKEN, WEBAPP_URL, FILES_DIR, MAX_FILE_SIZE
import os
import sys

child_dir = os.path.dirname(__file__)
parent_dir = os.path.abspath(os.path.join(child_dir, '..'))
sys.path.append(parent_dir)
import db
import tinkoff
from utils import finalize_chat_payment_success, _is_paid_status, _run_blocking

logger = setup_logging()

# Совместимость старых вызовов TemplateResponse(name, {"request": request, ...})
# с актуальной сигнатурой Starlette TemplateResponse(request, name, context).
_original_template_response = Jinja2Templates.TemplateResponse


def _template_response_compat(self, *args, **kwargs):
    if args and isinstance(args[0], str):
        name = args[0]
        context = args[1] if len(args) > 1 else kwargs.pop("context", {})
        context = dict(context or {})
        request = context.pop("request", None) or kwargs.pop("request", None)
        if request is None:
            raise ValueError("TemplateResponse requires a request object")
        return _original_template_response(
            self,
            request=request,
            name=name,
            context=context,
            **kwargs,
        )
    return _original_template_response(self, *args, **kwargs)


Jinja2Templates.TemplateResponse = _template_response_compat

from api import router

# In-memory rate limiting (для демо, в production используйте Redis)
request_counts = defaultdict(list)
blocked_ips: dict[str, float] = {}   # ip -> blocked_at timestamp
BLOCK_TTL = 300  # 5 минут
PAYMENT_RECONCILE_INTERVAL_SEC = 60
PAYMENT_RECONCILE_LOOKBACK_MIN = 24 * 60

class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.suspicious_paths = {
            '/login_up.php', '/dashboard', '/panel', '/xui',
            '/metrics', '/sdk', '/HNAP1', '/console'
        }
        self.suspicious_user_agents = [
            'nmap', 'sqlmap', 'nikto', 'metasploit',
            'nessus', 'acunetix', 'netsparker', 'w3af'
        ]
        # Безопасные polling GET-запросы чата — не учитываются в глобальном rate limit
        self.polling_get_path_re = re.compile(
            r'^/api/chats/[^/]+(?:/(?:messages|files|payment-current))?$'
        )

    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host
        current_time = time.time()

        # 1. Проверка на блокировку IP (с TTL — автоматическая разблокировка)
        if client_ip in blocked_ips:
            if current_time - blocked_ips[client_ip] < BLOCK_TTL:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too Many Requests"}
                )
            else:
                del blocked_ips[client_ip]

        is_polling_request = (
            request.method == 'GET'
            and self.polling_get_path_re.match(request.url.path) is not None
        )

        # 2. Rate limiting (100 запросов в минуту на IP)
        if not is_polling_request:
            request_counts[client_ip] = [
                t for t in request_counts[client_ip]
                if current_time - t < 60
            ]

            if len(request_counts[client_ip]) > 100:
                blocked_ips[client_ip] = current_time
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too Many Requests"}
                )

            request_counts[client_ip].append(current_time)

        # 3. Проверка подозрительных путей
        path = request.url.path
        if any(susp_path in path for susp_path in self.suspicious_paths):
            return JSONResponse(
                status_code=404,
                content={"detail": "Not Found"}
            )
        
        # 4. Проверка User-Agent
        user_agent = request.headers.get('user-agent', '').lower()
        if any(bot in user_agent for bot in self.suspicious_user_agents):
            blocked_ips[client_ip] = current_time
            return JSONResponse(
                status_code=403,
                content={"detail": "Forbidden"}
            )
        
        # 5. Ограничение времени обработки запроса
        # Для GET (polling/обновления чатов) не применяем жёсткий timeout,
        # чтобы не прерывать длинные запросы и не ловить aclose() гонки.
        if request.method == "GET":
            response = await call_next(request)
        else:
            try:
                response = await asyncio.wait_for(
                    call_next(request),
                    timeout=30.0  # 30 секунд максимум
                )
            except asyncio.TimeoutError:
                return JSONResponse(
                    status_code=504,
                    content={"detail": "Request Timeout"}
                )
        
        return response

async def _reconcile_pending_payments_once() -> None:
    try:
        rows = await db.list_chat_payments_awaiting_confirmation(max_age_minutes=PAYMENT_RECONCILE_LOOKBACK_MIN)
    except Exception:
        logger.warning("Payment reconcile: failed to list pending payments", exc_info=True)
        return

    for row in rows or []:
        payment_id = str(row.get("payment_id") or "").strip()
        if not payment_id:
            continue
        try:
            resp = await _run_blocking(tinkoff.get_payment_state, payment_id=payment_id)
            st = str((resp or {}).get("Status") or (resp or {}).get("status") or "").upper()
            if _is_paid_status(st):
                await finalize_chat_payment_success(payment_id=payment_id, source="reconcile")
        except Exception:
            logger.warning("Payment reconcile failed for payment_id=%s", payment_id, exc_info=True)

    try:
        repair_rows = await db.list_recent_confirmed_chat_payments(max_age_minutes=PAYMENT_RECONCILE_LOOKBACK_MIN)
    except Exception:
        logger.warning("Payment reconcile: failed to list confirmed payments for repair", exc_info=True)
        repair_rows = []

    for row in repair_rows:
        payment_id = str(row.get("payment_id") or "").strip()
        if not payment_id:
            continue
        try:
            await finalize_chat_payment_success(payment_id=payment_id, source="reconcile_repair")
        except Exception:
            logger.warning("Payment reconcile repair failed for payment_id=%s", payment_id, exc_info=True)


async def _payment_reconcile_loop() -> None:
    logger.info("Payment reconcile loop started (interval=%ss)", PAYMENT_RECONCILE_INTERVAL_SEC)
    while True:
        await _reconcile_pending_payments_once()
        await asyncio.sleep(PAYMENT_RECONCILE_INTERVAL_SEC)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting application...")
    
    # Обработка сигналов
    def shutdown_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down gracefully...")
    
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    
    # если у тебя миграции разделены
    logger.info("Database schemas checked")

    payment_reconcile_task = asyncio.create_task(_payment_reconcile_loop())

    yield

    # Shutdown
    payment_reconcile_task.cancel()
    try:
        await payment_reconcile_task
    except asyncio.CancelledError:
        pass
    logger.info("Shutting down...")

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
# Middleware для логирования запросов
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    
    # Получаем тело запроса для POST/PUT запросов
    body = None
    if request.method in ["POST", "PUT"] and "upload" not in str(request.url):
        try:
            body_bytes = await request.body()
            body = body_bytes.decode('utf-8')[:1000]
            async def receive():
                return {'type': 'http.request', 'body': body_bytes}
            request._receive = receive
        except:
            body = "<binary or large data>"
    
    response = await call_next(request)
    
    process_time = time.time() - start_time
    log_message = f"{request.method} {request.url.path} - Status: {response.status_code} - Time: {process_time:.3f}s"
    
    if body:
        log_message += f" - Body: {body[:500]}..." if len(body) > 500 else f" - Body: {body}"
    
    if response.status_code >= 400:
        logger.error(log_message)
    else:
        logger.info(log_message)
    
    return response

# Добавляем middleware безопасности
app.add_middleware(SecurityMiddleware)

# Добавляем CORS
_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключаем роутеры
app.include_router(router)

# Health check endpoint
@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": time.time()}
