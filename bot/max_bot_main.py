"""
max_bot_main.py — Основной цикл MAX-бота.

Работает по принципу long polling:
1. Вызываем api.get_events() — сервер MAX держит соединение ~30 сек
2. Если пришли события — раздаём их зарегистрированным обработчикам
3. Повторяем

Обработчики регистрируются как функции:
- on_message(command="/start")  -> обработчик команды /start
- on_message()                  -> обработчик любого текста
- on_callback(pattern="menu_*") -> обработчик нажатий кнопок с payload=menu_*

Это аналог telegram.ext.Application, но проще и заточен под MAX API.
"""

import logging
import asyncio
import signal
import fnmatch
import datetime
import time as _time
import hashlib
from collections import OrderedDict
from typing import Callable, Awaitable, Optional
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv
import os
import sys

# Добавляем корневую директорию проекта в path
_bot_dir = Path(__file__).resolve().parent
_project_dir = _bot_dir.parent
sys.path.insert(0, str(_project_dir))
sys.path.insert(0, str(_bot_dir))

load_dotenv(_project_dir / "inf.env")

from max_api import MaxBotAPI, MaxEvent, MaxMessage, MaxCallback, InlineKeyboard, InlineButton

# Import log_event for analytics
from db import log_event

logger = logging.getLogger(__name__)
_log_dir = _project_dir / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
_max_log_file = _log_dir / "max_bot.log"
_max_legacy_log_file = _project_dir / "bot_max.log"  # backward compatibility

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_max_log_file, encoding="utf-8"),
        logging.FileHandler(_max_legacy_log_file, encoding="utf-8"),
    ],
    force=True,
)
logger = logging.getLogger(__name__)
logger.info("MAX bot logging initialized: file=%s legacy_file=%s", _max_log_file, _max_legacy_log_file)

# Тип обработчика
MessageHandler = Callable[["MaxBotContext", MaxMessage], Awaitable[None]]
CallbackHandler = Callable[["MaxBotContext", MaxCallback], Awaitable[None]]
JobHandler = Callable[["MaxBot"], Awaitable[None]]


class UserData(dict):
    """
    Хранилище данных пользователя (аналог context.user_data в python-telegram-bot).
    Просто dict с дополнительным удобством.
    """
    pass


class MaxBotContext:
    """
    Контекст, передаваемый в каждый обработчик.

    Содержит:
    - api: экземпляр MaxBotAPI для отправки ответов
    - user_data: dict с данными текущего пользователя (сохраняется между вызовами)
    - bot_data: общий dict для всего бота
    """

    def __init__(self, api: MaxBotAPI, user_data: UserData, bot_data: dict,
                 user_id: Optional[int] = None):
        self.api = api
        self.user_data = user_data
        self.bot_data = bot_data
        self.user_id = user_id


class MaxBot:
    """
    Ядро MAX-бота: long polling + маршрутизация событий.

    Использование:
        bot = MaxBot(token="...")
        bot.register_command("/start", handle_start)
        bot.register_message(handle_text)
        bot.register_callback("menu_*", handle_menu)
        await bot.run()
    """

    def __init__(self, token: str):
        self.api = MaxBotAPI(token=token)
        self._running = False

        # ID самого бота (заполняется в run() после get_me)
        self.bot_user_id: int = 0

        # Дедупликация событий: MAX API может присылать одно событие дважды
        # (at-least-once delivery). Храним последние N ключей обработанных событий.
        self._seen_events: OrderedDict[str, float] = OrderedDict()
        self._seen_events_max = 500

        # Обработчики команд: {"/start": handler, "/help": handler, ...}
        self._command_handlers: dict[str, MessageHandler] = {}

        # Обработчик произвольных текстовых сообщений (fallback)
        self._message_handler: Optional[MessageHandler] = None

        # Обработчики callback-ов: [(pattern, handler), ...]
        # pattern — строка с wildcard, например "menu_*" или "*"
        self._callback_handlers: list[tuple[str, CallbackHandler]] = []

        # Хранилище данных пользователей: {user_id: UserData}
        self._user_data: dict[int, UserData] = {}

        # Общие данные бота
        self.bot_data: dict = {}

        # Периодические задачи: [(coro_factory, interval_sec, name, first_delay)]
        self._repeating_jobs: list[tuple[JobHandler, float, str, float]] = []

        # Ежедневные задачи: [(coro_factory, hour, minute, tz, name)]
        self._daily_jobs: list[tuple[JobHandler, int, int, Optional[datetime.timezone], str]] = []

        # Активные asyncio-таски (для graceful shutdown)
        self._job_tasks: list[asyncio.Task] = []
        self._event_tasks: set[asyncio.Task] = set()

        # Ограниченный параллелизм: разные пользователи могут обрабатываться
        # одновременно, но не бесконечно.
        self._event_parallel_limit = 16
        self._event_semaphore = asyncio.Semaphore(self._event_parallel_limit)

        # Сохраняем строгую последовательность для одного пользователя.
        self._user_event_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ── Регистрация обработчиков ──────────────────────────────────

    def register_command(self, command: str, handler: MessageHandler) -> None:
        """
        Зарегистрировать обработчик команды.
        command: "/start", "/help" и т.д.
        """
        if not command.startswith("/"):
            command = "/" + command
        self._command_handlers[command] = handler
        logger.info("Registered command handler: %s", command)

    def register_message(self, handler: MessageHandler) -> None:
        """
        Зарегистрировать обработчик для всех текстовых сообщений
        (которые не являются командами).
        """
        self._message_handler = handler
        logger.info("Registered fallback message handler")

    def register_callback(self, pattern: str, handler: CallbackHandler) -> None:
        """
        Зарегистрировать обработчик callback-нажатий.
        pattern: строка с wildcard, например "menu_*", "cart", "*"
        """
        self._callback_handlers.append((pattern, handler))
        logger.info("Registered callback handler: %s", pattern)

    # ── Регистрация периодических задач ───────────────────────────

    def register_job(
        self,
        handler: JobHandler,
        interval: float,
        name: str = "",
        first: float = 10,
    ) -> None:
        """
        Зарегистрировать периодическую задачу.
        handler: async def job(bot: MaxBot) -> None
        interval: интервал в секундах между вызовами
        first: задержка перед первым запуском (сек)
        """
        self._repeating_jobs.append((handler, interval, name or handler.__name__, first))
        logger.info("Registered repeating job: %s (every %ss)", name or handler.__name__, interval)

    def register_daily_job(
        self,
        handler: JobHandler,
        hour: int = 0,
        minute: int = 0,
        tz: Optional[datetime.timezone] = None,
        name: str = "",
    ) -> None:
        """
        Зарегистрировать ежедневную задачу.
        handler: async def job(bot: MaxBot) -> None
        hour, minute: время запуска
        tz: timezone (по умолчанию UTC)
        """
        self._daily_jobs.append((handler, hour, minute, tz, name or handler.__name__))
        logger.info("Registered daily job: %s (at %02d:%02d)", name or handler.__name__, hour, minute)

    # ── Получение UserData ────────────────────────────────────────

    def _get_user_data(self, user_id: int) -> UserData:
        if user_id not in self._user_data:
            self._user_data[user_id] = UserData()
        return self._user_data[user_id]

    # ── Analytics helper ──────────────────────────────────────────

    async def _safe_log_event(self, **kwargs):
        """Non-blocking analytics logging (swallows errors)."""
        try:
            await log_event(**kwargs)
        except BaseException as e:
            try:
                logger.debug("log_event failed: %s", e)
            except Exception:
                pass

    # ── Маршрутизация событий ─────────────────────────────────────

    async def _dispatch_message(self, msg: MaxMessage) -> None:
        """Определяем, это команда или обычный текст, и вызываем нужный обработчик."""
        if not msg.sender:
            return

        uid = msg.sender.user_id

        # Игнорируем собственные сообщения бота (предотвращаем каскад)
        if self.bot_user_id and uid == self.bot_user_id:
            logger.debug("skip own message: mid=%s", msg.message_id)
            return
        ctx = MaxBotContext(self.api, self._get_user_data(uid), self.bot_data, user_id=uid)

        # Всегда сохраняем chat_id из входящих сообщений для использования в callback'ах
        if msg.chat_id and str(msg.chat_id).strip():
            ctx.user_data["_chat_id"] = str(msg.chat_id).strip()

        text = (msg.text or "").strip()

        # ── Analytics: log message event ──
        has_file = bool(
            getattr(msg, "document", None) or getattr(msg, "photo", None) or 
            getattr(msg, "video", None) or getattr(msg, "audio", None) or 
            getattr(msg, "voice", None) or getattr(msg, "file", None)
        )
        name = "command" if text.startswith("/") else "text"
        
        asyncio.create_task(self._safe_log_event(
            event_type="message",
            max_user_id=uid,
            chat_id=getattr(msg, "chat_id", None),
            message_id=getattr(msg, "message_id", None),
            name=name,
            text_len=len(text) if text else 0,
            has_file=has_file,
            payload={
                "has_file": has_file,
            },
        ))

        # Проверяем, это команда?
        if text.startswith("/"):
            # Берём первое слово как команду: "/start foo bar" -> "/start"
            cmd = text.split()[0].lower()
            handler = self._command_handlers.get(cmd)
            if handler:
                try:
                    await handler(ctx, msg)
                except Exception:
                    logger.exception("Error in command handler %s", cmd)
                return

        # Не команда — пробуем fallback
        if self._message_handler:
            try:
                await self._message_handler(ctx, msg)
            except Exception:
                logger.exception("Error in message handler")

    async def _dispatch_callback(self, cb: MaxCallback) -> None:
        """Находим подходящий обработчик по payload (с учётом wildcard)."""
        logger.info(
            "dispatch_callback: callback_id=%s, payload=%s, user=%s",
            cb.callback_id, cb.payload, cb.user.user_id if cb.user else None,
        )

        if not cb.user:
            logger.warning(
                "dispatch_callback: cb.user is None! Пробуем извлечь user_id из message.sender"
            )
            # Fallback: берём user из message.sender (если кнопка была в сообщении)
            if cb.message and cb.message.sender:
                cb.user = cb.message.sender
                logger.info("dispatch_callback: восстановили user из message.sender: %s", cb.user.user_id)
            else:
                logger.error("dispatch_callback: не удалось определить user, пропускаем callback")
                return

        uid = cb.user.user_id
        ctx = MaxBotContext(self.api, self._get_user_data(uid), self.bot_data, user_id=uid)

        # Сохраняем chat_id из callback-сообщения, чтобы последующие хендлеры
        # не теряли получателя даже если будут опираться на user_data fallback.
        if cb.message and cb.message.chat_id and str(cb.message.chat_id).strip():
            ctx.user_data["_chat_id"] = str(cb.message.chat_id).strip()
            logger.info(
                "dispatch_callback: saved chat_id=%s for user=%s",
                ctx.user_data["_chat_id"], uid,
            )

        # Отвечаем на callback, чтобы MAX убрал "часики" у пользователя
        try:
            await self.api.answer_callback(cb.callback_id)
        except Exception:
            logger.warning("answer_callback failed for callback_id=%s", cb.callback_id)

        payload = cb.payload or ""

        # ── Analytics: log callback event ──
        name = payload.split(":", 1)[0] if payload else None
        
        asyncio.create_task(self._safe_log_event(
            event_type="callback",
            max_user_id=uid,
            chat_id=getattr(cb.message, "chat_id", None) if cb.message else None,
            message_id=getattr(cb.message, "message_id", None) if cb.message else None,
            name=name,
            callback_data=payload,
            payload={
                "in_chat_system": payload.startswith(("cc_", "ag_", "client_chat_start:")),
            },
        ))

        # ── Debounce кликов (защита от двойного нажатия) ──
        ud = ctx.user_data
        now = _time.time()
        last_ts = ud.get("_last_cb_ts", 0.0)
        last_data = ud.get("_last_cb_data")
        if payload == last_data and (now - last_ts) < 2.0:
            logger.debug("debounce: skip duplicate callback %s", payload)
            return
        ud["_last_cb_ts"] = now
        ud["_last_cb_data"] = payload

        for pattern, handler in self._callback_handlers:
            if fnmatch.fnmatch(payload, pattern):
                logger.info("dispatch_callback: matched pattern=%s for payload=%s", pattern, payload)
                try:
                    await handler(ctx, cb)
                except Exception:
                    logger.exception("Error in callback handler (pattern=%s)", pattern)
                return

        logger.warning("No callback handler matched payload: %s", payload)

    def _event_dedup_key(self, event: MaxEvent) -> str:
        """Генерирует уникальный ключ события для дедупликации."""
        raw = event.raw
        # message_created: mid уникален
        if event.event_type == "message_created" and event.message:
            mid = event.message.message_id
            if mid:
                return f"msg:{mid}"
            # Синтетический /start из bot_started — нет mid, используем timestamp+user
            ts = raw.get("timestamp", "")
            uid = raw.get("user_id", "") or raw.get("user", {}).get("user_id", "")
            return f"bot_started:{uid}:{ts}"
        # message_callback: callback_id уникален
        if event.event_type == "message_callback" and event.callback:
            return f"cb:{event.callback.callback_id}"
        # Fallback: хэш всего raw события
        raw_str = str(sorted(raw.items())) if isinstance(raw, dict) else str(raw)
        return f"raw:{hashlib.md5(raw_str.encode()).hexdigest()}"

    def _is_duplicate_event(self, event: MaxEvent) -> bool:
        """Проверяет, было ли это событие уже обработано."""
        key = self._event_dedup_key(event)
        if key in self._seen_events:
            logger.info("DEDUP: skip duplicate event key=%s type=%s", key, event.event_type)
            return True
        self._seen_events[key] = _time.time()
        # Ограничиваем размер кэша
        while len(self._seen_events) > self._seen_events_max:
            self._seen_events.popitem(last=False)
        return False

    async def _process_event(self, event: MaxEvent) -> None:
        """Обработать одно событие."""
        if event.event_type == "message_created" and event.message:
            await self._dispatch_message(event.message)
        elif event.event_type == "message_callback" and event.callback:
            await self._dispatch_callback(event.callback)
        else:
            logger.debug("Skipping event type: %s", event.event_type)

    def _get_event_user_id(self, event: MaxEvent) -> Optional[int]:
        """Пытается извлечь user_id из event для per-user serialization."""
        if event.event_type == "message_created" and event.message and event.message.sender:
            return event.message.sender.user_id
        if event.event_type == "message_callback" and event.callback:
            if event.callback.user:
                return event.callback.user.user_id
            if event.callback.message and event.callback.message.sender:
                return event.callback.message.sender.user_id
        return None

    def _track_event_task(self, task: asyncio.Task) -> None:
        self._event_tasks.add(task)

        def _on_done(done_task: asyncio.Task) -> None:
            self._event_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Unhandled exception in event task")

        task.add_done_callback(_on_done)

    async def _handle_event_task(self, event: MaxEvent, user_id: Optional[int]) -> None:
        """Единица конкурентной обработки event с ограничением и per-user lock."""
        async with self._event_semaphore:
            if user_id is None:
                await self._process_event(event)
                return
            async with self._user_event_locks[user_id]:
                await self._process_event(event)

    async def _cancel_event_tasks(self) -> None:
        if not self._event_tasks:
            return
        pending_tasks = list(self._event_tasks)
        for task in pending_tasks:
            task.cancel()
        await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._event_tasks.clear()

    # ── Job runners ─────────────────────────────────────────────────

    async def _run_repeating_job(
        self, handler: JobHandler, interval: float, name: str, first: float
    ) -> None:
        """Цикл: ждём first секунд, затем вызываем handler каждые interval секунд."""
        await asyncio.sleep(first)
        while self._running:
            try:
                await handler(self)
            except Exception:
                logger.exception("Error in repeating job '%s'", name)
            # Спим interval секунд, но просыпаемся при остановке
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                break  # stop_event сработал
            except asyncio.TimeoutError:
                pass  # нормальный таймаут — следующая итерация

    async def _run_daily_job(
        self,
        handler: JobHandler,
        hour: int,
        minute: int,
        tz: Optional[datetime.timezone],
        name: str,
    ) -> None:
        """Ждём нужного времени каждый день и вызываем handler."""
        tz = tz or datetime.timezone.utc
        while self._running:
            now = datetime.datetime.now(tz)
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
            delay = (target - now).total_seconds()
            logger.info("Daily job '%s': next run in %.0f sec (%s)", name, delay, target.strftime("%H:%M %Z"))

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                break  # остановка
            except asyncio.TimeoutError:
                pass  # пора запускать

            if not self._running:
                break
            try:
                await handler(self)
            except Exception:
                logger.exception("Error in daily job '%s'", name)

    def _start_jobs(self) -> None:
        """Запустить все зарегистрированные jobs как asyncio tasks."""
        for handler, interval, name, first in self._repeating_jobs:
            task = asyncio.create_task(
                self._run_repeating_job(handler, interval, name, first),
                name=f"job:{name}",
            )
            self._job_tasks.append(task)

        for handler, hour, minute, tz, name in self._daily_jobs:
            task = asyncio.create_task(
                self._run_daily_job(handler, hour, minute, tz, name),
                name=f"daily:{name}",
            )
            self._job_tasks.append(task)

    async def _cancel_jobs(self) -> None:
        """Остановить все job-таски."""
        for task in self._job_tasks:
            task.cancel()
        if self._job_tasks:
            await asyncio.gather(*self._job_tasks, return_exceptions=True)
        self._job_tasks.clear()

    # ── Главный цикл ─────────────────────────────────────────────

    async def run(self) -> None:
        """
        Запустить бота (long polling + jobs).
        Работает бесконечно, пока не получит сигнал остановки.
        """
        await self.api.start()

        # Проверяем токен
        me = await self.api.get_me()
        if not me or not me.get("user_id"):
            logger.error("Не удалось получить информацию о боте. Проверьте токен! Ответ: %s", me)
            await self.api.close()
            return

        self.bot_user_id = int(me.get("user_id", 0))
        bot_name = me.get("first_name") or me.get("name") or me.get("username", "???")
        logger.info("MAX-бот запущен: %s (user_id=%s)", bot_name, self.bot_user_id)

        self._running = True
        self._stop_event = asyncio.Event()

        # Обработка сигналов остановки
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop)
            except NotImplementedError:
                pass  # Windows

        # Запускаем периодические задачи
        self._start_jobs()

        try:
            while self._running:
                try:
                    events = await self.api.get_events(poll_time=20)
                    for event in events:
                        if self._is_duplicate_event(event):
                            continue
                        user_id = self._get_event_user_id(event)
                        task = asyncio.create_task(
                            self._handle_event_task(event, user_id),
                            name=f"event:{event.event_type}:{user_id or 'unknown'}",
                        )
                        self._track_event_task(task)
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("Ошибка в цикле long polling")
                    await asyncio.sleep(3)  # пауза перед повтором
        finally:
            await self._cancel_event_tasks()
            await self._cancel_jobs()
            logger.info("MAX-бот остановлен")
            await self.api.close()

    def _stop(self) -> None:
        """Сигнал остановки."""
        logger.info("Получен сигнал остановки...")
        self._running = False
        self._stop_event.set()


# ── Точка входа ───────────────────────────────────────────────────────
def main():
    """Запуск MAX-бота."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    token = os.getenv("MAX_BOT_TOKEN", "")
    if not token:
        logger.error("MAX_BOT_TOKEN не задан! Установите переменную окружения или добавьте в inf.env")
        sys.exit(1)

    bot = MaxBot(token=token)

    # Подключаем обработчики
    from max_handlers import register_handlers
    register_handlers(bot)

    # Ежедневные уведомления (ОФД/ФН/маркировка/коды) — 15:15 МСК
    MOSCOW_TZ = datetime.timezone(datetime.timedelta(hours=3))

    async def _daily_ofd_reminder(bot_instance: "MaxBot") -> None:
        from max_notifications import max_ofd_reminder_job
        await max_ofd_reminder_job(bot_instance.api)

    bot.register_daily_job(
        _daily_ofd_reminder,
        hour=15, minute=15,
        tz=MOSCOW_TZ,
        name="max_ofd_daily_reminder",
    )

    # Фоновое уведомление специалистов о новых заявках (каждые 30 сек)
    async def _pending_notifier(bot_instance: "MaxBot") -> None:
        from max_agent_chat import max_support_pending_notifier
        await max_support_pending_notifier(bot_instance)

    bot.register_job(_pending_notifier, interval=30, name="max_support_pending_notifier", first=15)

    # Автопроверка оплаты (каждые 30 сек, аналог auto_check_payment_job из TG)
    async def _auto_check_payment(bot_instance: "MaxBot") -> None:
        from max_catalog import auto_check_payment_job
        await auto_check_payment_job(bot_instance)

    bot.register_job(_auto_check_payment, interval=30, name="auto_check_payment", first=30)

    # Миграция схемы — добавить max_user_id в specialists (одноразово)
    async def _ensure_schema(bot_instance: "MaxBot") -> None:
        from db import ensure_specialist_max_column
        await ensure_specialist_max_column()

    bot.register_job(_ensure_schema, interval=999999, name="ensure_specialist_max_column", first=2)

    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
