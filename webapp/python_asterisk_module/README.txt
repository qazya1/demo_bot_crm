Что изменено относительно старого модуля:
- убраны зависимости от Django ORM, transaction.atomic и timezone
- убраны Django models, вместо них Pydantic-схемы и абстракция репозитория
- build_telephony_service теперь принимает repository и ari_config извне
- по умолчанию больше нет захардкоженных паролей/пустого caller ID
- модуль пригоден для FastAPI webapp и не зависит от конкретной ORM

Найденные ошибки/слабые места в старом модуле:
1) жёсткая зависимость от Django (django.db.transaction, django.utils.timezone, models.Model)
2) импорты `from ari_client import ...` и `from models import ...` ломкие для пакетного использования
3) build_telephony_service захардкоживал ARI credentials, trunk и пустой default_callerid
4) build_telephony_service при каждом вызове создавал подключение, вместо singleton/lifespan-паттерна
5) trunk_endpoint по умолчанию указывал на verify-trunk, а не на боевой
6) модели описывали ORM-слой, а не контракт модуля для внешнего кода

Состав:
- ari_client.py — ARI client + controller двух ног звонка
- schemas.py — Pydantic-схемы домена
- repository.py — интерфейс репозитория, который реализуешь на своей БД/ORM
- service.py — чистый сервисный слой под FastAPI
- fastapi_example.py — пример интеграции в FastAPI через lifespan

Установка:
pip install requests websocket-client fastapi pydantic uvicorn

Как использовать во внешнем коде:
1) реализовать TelephonyRepository под свою БД (SQLAlchemy, Tortoise, raw SQL и т.д.)
2) создать AriConfig из env/конфига
3) один раз на старте FastAPI вызвать build_telephony_service(repository=..., ari_config=...)
4) хранить сервис в app.state и доставать через Depends
5) вызывать service.start_call(...) и service.cancel_call(...)
6) на shutdown закрывать websocket через service.ari_controller.ari.disconnect_events()

Минимальный пример внешнего использования:

from ari_client import AriConfig
from service import build_telephony_service

repo = MyRepository(...)
ari_cfg = AriConfig(
    base_url="http://127.0.0.1:8088/ari",
    ws_url="ws://127.0.0.1:8088/ari/events",
    username="ari_user",
    password="...",
    app_name="bot-crm-call-control",
    trunk_endpoint="example-endpoint",
    default_callerid="79000000000",
)
service = build_telephony_service(repository=repo, ari_config=ari_cfg)

session = service.start_call(
    initiator_abonent_id=1,
    caller_abonent_id=1,
    callee_abonent_id=2,
    initiator_type="user",
    callerid="79000000000",
)

Важно:
- не создавай сервис на каждый HTTP-запрос
- поднимай его один раз на старте приложения
- verify-trunk используй только пока рабочий тестовый trunk у тебя именно он
