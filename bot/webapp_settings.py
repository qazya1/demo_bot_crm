import os


WEBAPP_URL = os.getenv("WEBAPP_URL", "https://127.0.0.1:8443").rstrip("/")

# Публичное имя MAX-бота (username без @).
# Используется для формирования open_app кнопок через MAX deep-link:
#   https://max.ru/<MAX_BOT_NAME>?startapp=<token>
# Токен передаётся в мини-приложение через Bridge: window.WebApp.initDataUnsafe.start_param
# Пример значения: MAX_BOT_NAME=yurtacrm_bot
MAX_BOT_NAME = os.getenv("MAX_BOT_NAME", "")


# Username TG-бота для реферальных deep-link (без @).
TG_REF_BOT_USERNAME = os.getenv("TG_REF_BOT_USERNAME", "").lstrip("@")

# Имя MAX-бота для реферальных deep-link (без @).
# Если пусто, будет использован MAX_BOT_NAME.
MAX_REF_BOT_NAME = os.getenv("MAX_REF_BOT_NAME", "").strip().lstrip("@")

# Опциональный fallback для MAX referral deep-link, если MAX_BOT_NAME/MAX_REF_BOT_NAME не заданы.
MAX_REF_FALLBACK_BOT_NAME = os.getenv("MAX_REF_FALLBACK_BOT_NAME", "").strip().lstrip("@")
