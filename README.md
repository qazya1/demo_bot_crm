# BOT CRM — омниканальная CRM, сервисная платформа и бизнес-бэкенд

> Единая система для клиентов, компаний, партнёров и сотрудников: Django CRM, Telegram- и MAX-боты, FastAPI WebApp, поддержка, телефония Asterisk, каталог, платежи, заказы, задачи, ККТ, реферальная программа, лояльность, финансовый контур и внешние интеграции.

![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)
![Django](https://img.shields.io/badge/Django-4.2.9-092E20?logo=django&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-WebApp-009688?logo=fastapi&logoColor=white)
![MySQL](https://img.shields.io/badge/MySQL-utf8mb4-4479A1?logo=mysql&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-Bot-26A5E4?logo=telegram&logoColor=white)
![MAX](https://img.shields.io/badge/MAX-Bot-6C5CE7)
![Status](https://img.shields.io/badge/status-active_development-orange)

## Содержание

- [О проекте](#о-проекте)
- [Масштаб системы](#масштаб-системы)
- [Архитектура](#архитектура)
- [Точки входа и процессы](#точки-входа-и-процессы)
- [Карта репозитория](#карта-репозитория)
- [Ключевые backend-решения](#ключевые-backend-решения)
  - [Единая идентичность клиента](#1-единая-идентичность-клиента-в-telegram-max-crm-и-webapp)
  - [Клиенты, контакты и компании](#2-клиенты-контакты-компании-и-ккт)
  - [Задачи и workflow](#3-задачи-как-отдельный-бизнес-процесс)
  - [Омниканальная поддержка](#4-омниканальная-поддержка-и-жизненный-цикл-чата)
  - [Телефония Asterisk ARI](#5-телефония-asterisk-ari)
  - [Заказы, склад и платежи](#6-заказы-склад-платежи-и-post-payment-pipeline)
  - [Лояльность и финансы](#7-лояльность-рефералы-партнёры-и-финансовый-контур)
  - [Каталог и внешние продукты](#8-каталог-ценообразование-и-внешние-продукты)
  - [Альфа-Банк Partner API](#9-собственный-sdk-для-альфа-банк-partner-api)
  - [Уведомления, аналитика и фоновые процессы](#10-уведомления-аналитика-и-фоновые-процессы)
- [Модель данных](#модель-данных)
- [Роли и контроль доступа](#роли-и-контроль-доступа)
- [Надёжность и безопасность](#надёжность-и-безопасность)
- [Основные маршруты](#основные-маршруты)
- [Локальный запуск](#локальный-запуск)
- [Тестирование](#тестирование)
- [Правила развития проекта](#правила-развития-проекта)
- [Автор](#автор)

## О проекте

**BOT CRM** — это не «CRM рядом с ботом», а единый backend-контур, в котором несколько приложений работают с одними клиентами, компаниями, заказами, обращениями и финансовыми событиями.

Клиент может начать взаимодействие в Telegram, продолжить в MAX, открыть защищённый WebApp-чата, оплатить заказ, получить код или услугу, обратиться к специалисту и участвовать в программе лояльности. Сотрудник видит того же клиента в CRM, получает задачу, ведёт чат, связывается по телефону и работает с заказом. Директор управляет распределением обращений, специалистами, партнёрами, аналитикой, ценами и финансовыми операциями.

Главная сложность проекта — не количество экранов, а согласование нескольких параллельных процессов:

- одна сущность клиента имеет внутренний ID и идентификаторы разных мессенджеров;
- бот, CRM, WebApp и фоновые jobs должны идемпотентно обрабатывать одно событие;
- заказ должен пройти резервирование, оплату, выдачу, списание остатков, создание CRM-события и начисление баллов;
- чат имеет собственные роли, токены доступа, файлы, платежи, распределение, закрытие и автозакрытие;
- компания содержит несколько контактов и ККТ, при этом доступ партнёра и специалиста должен оставаться ограниченным;
- телефонный звонок состоит из двух независимых каналов Asterisk, которые нужно корректно соединить и завершить.

## Масштаб системы

Это один репозиторий, но несколько самостоятельных приложений и доменов.

| Часть | Реальный объём и ответственность |
|---|---|
| Django CRM | Доменные views и services для клиентов, компаний, чатов, заказов, задач, партнёров, аналитики и финансов |
| Модель данных | 37 Django-проекций таблиц с 'managed = False' плюс вспомогательные таблицы и SQL-миграции |
| Telegram | Оркестратор около 1 950 строк; регистрация около 2 865; каталог/checkout около 3 450; клиентский чат около 2 250 |
| MAX | Собственный polling/router и отдельные большие модули регистрации, каталога, заказов, чатов и уведомлений |
| Асинхронный DB layer | 'db/chats.py' около 3 060 строк, 'db/orders.py' около 1 620, 'db/users.py' около 1 350 |
| CRM workflow | Чаты около 1 560 строк; финансы/лояльность около 1 860; задачи: view + service около 1 680 |
| FastAPI | WebApp-чата, файловое API, платежи, security middleware и reconciliation loop |
| Телефония | Собственный ARI client/controller, двухногий звонок, bridge, события и классификация исхода |
| Банковская интеграция | Отдельный sync/async Python SDK Альфа-Банка с transport-слоем, Pydantic-моделями и тестами |
| Миграции | Последовательные SQL-изменения для чатов, платежей, лояльности, задач, партнёров, индексов и FK |

Цифры приведены не как метрика качества, а чтобы показать, что каждый блок — отдельная подсистема, а не один обработчик или страница.

## Архитектура

~~~mermaid
flowchart TB
    CHANNELS["Каналы клиента<br/>Telegram · MAX · WebApp"]
    SERVICES["Бизнес-сервисы<br/>боты · чат · заказы · телефония"]
    DB[("MySQL<br/>единое состояние")]
    CRM["Django CRM<br/>директор · специалист · партнёр"]
    EXT["Внешние системы<br/>T-Bank · ОФД · Альфа-Банк · Asterisk"]

    CHANNELS --> SERVICES
    SERVICES <--> DB
    CRM <--> DB
    SERVICES <--> EXT
    CRM --> SERVICES
~~~

### Почему в проекте два способа работы с базой

**Django-контур** использует ORM и прямой SQL для серверных страниц и API CRM. Большинство моделей отражает существующую схему через 'managed = False'.

**Асинхронный контур** в каталоге [db/](db/) построен поверх 'mysql.connector.aio'. Его используют Telegram, MAX, FastAPI и фоновые процессы. В нём реализованы:

- пул соединений;
- проверка живого соединения и rollback перед возвратом в pool;
- транзакционные операции;
- повторное подключение при сетевых ошибках;
- cursor proxy;
- переключаемый тестовый контур через автоматическое переписывание имён таблиц;
- общие проверки наличия таблиц, колонок и индексов.

Это разделение позволяет Django работать как CRM, а асинхронным приложениям не блокировать polling, HTTP-вызовы и параллельные пользовательские сценарии.

## Точки входа и процессы

| Команда / файл | Что запускается |
|---|---|
| 'python manage.py runserver' | Django CRM и серверные JSON API |
| 'python run_bot.py' | Telegram-бот |
| 'python run_max_bot.py' | MAX-бот |
| 'uvicorn webapp.main:app ...' | FastAPI WebApp, чат, платежи и Web API |
| 'python run_chat_cron.py' | автозакрытие зависших обращений и уведомления |
| 'python manage.py collectstatic' | сборка статики CRM |
| 'gunicorn wsgi:application ...' | production WSGI-процесс Django |

В production эти процессы должны управляться отдельно через systemd, supervisor или контейнерный orchestrator. Ошибка одного bot-процесса не должна останавливать CRM и наоборот.

## Карта репозитория

Ниже — навигация для разработчика: куда идти за конкретной логикой.

### Корень

| Путь | Назначение |
|---|---|
| [manage.py](manage.py) | Django CLI |
| [settings.py](settings.py) | Django, MySQL, cookies, CSRF, logging, static/media |
| [urls.py](urls.py) | корневой URLConf |
| [requirements.txt](requirements.txt) | общие зависимости |
| [run_bot.py](run_bot.py) | Telegram entrypoint |
| [run_max_bot.py](run_max_bot.py) | MAX entrypoint |
| [run_chat_cron.py](run_chat_cron.py) | фоновые правила закрытия чатов |
| [tinkoff.py](tinkoff.py) | T-Bank API, подписи и webhook validation |
| [migrations/](migrations/) | SQL-миграции основной базы |

### Django CRM — bot_app

| Путь | Ответственность |
|---|---|
| [bot_app/urls.py](bot_app/urls.py) | полная карта CRM и API-маршрутов |
| [bot_app/models.py](bot_app/models.py) | 37 отражений доменных таблиц |
| [bot_app/permissions.py](bot_app/permissions.py) | роли и объектные ограничения доступа |
| [bot_app/task_workflow.py](bot_app/task_workflow.py) | каноничный state machine задач |
| [bot_app/views/\_\_init\_\_.py](bot_app/views/__init__.py) | явная карта экспортов после модульного рефакторинга |
| [bot_app/views/auth.py](bot_app/views/auth.py) | вход, hash migration, rate limit попыток |
| [bot_app/views/dashboard.py](bot_app/views/dashboard.py) | директорский dashboard и аналитика |
| [bot_app/views/clients.py](bot_app/views/clients.py) | клиенты, карточка, заметки, custom fields, ККТ |
| [bot_app/views/companies.py](bot_app/views/companies.py) | компании, контакты и ККТ компании |
| [bot_app/views/orders.py](bot_app/views/orders.py) | заказы, ручные сделки, статистика, Excel |
| [bot_app/views/tasks.py](bot_app/views/tasks.py) | список задач, фильтры, синхронизация, статистика |
| [bot_app/views/chats.py](bot_app/views/chats.py) | CRM-чаты, файлы, платежи, transfer/close/read state |
| [bot_app/views/partner.py](bot_app/views/partner.py) | изолированный кабинет партнёра |
| [bot_app/views/notifications.py](bot_app/views/notifications.py) | центр уведомлений и сроки |
| [bot_app/views/calendar.py](bot_app/views/calendar.py) | события и категории календаря |
| [bot_app/views/settings.py](bot_app/views/settings.py) | runtime-настройки, продукты и цены |
| [bot_app/views/bot_api.py](bot_app/views/bot_api.py) | Bearer API для событий от ботов |
| [bot_app/views_loyalty.py](bot_app/views_loyalty.py) | финансовый блок и аналитика лояльности |
| [bot_app/services/client_service.py](bot_app/services/client_service.py) | операции над клиентом и ККТ |
| [bot_app/services/company_service.py](bot_app/services/company_service.py) | операции над компаниями, контактами и ККТ |
| [bot_app/services/order_service.py](bot_app/services/order_service.py) | ручные сделки и изменение заказов |
| [bot_app/services/task_service.py](bot_app/services/task_service.py) | workflow, история и связи задач с клиентами |
| [bot_app/templates/bot_app/](bot_app/templates/bot_app/) | интерфейсы CRM и общая sidebar-навигация |

### Telegram и MAX

| Путь | Ответственность |
|---|---|
| [bot/bot_main.py](bot/bot_main.py) | Telegram ConversationHandler, jobs и общий router |
| [bot/bot_registration.py](bot/bot_registration.py) | регистрация, согласие, профиль, верификация, заявки |
| [bot/bot_catalog.py](bot/bot_catalog.py) | каталог, корзина, склад, checkout, оплата и выдача |
| [bot/bot_client_chat.py](bot/bot_client_chat.py) | клиентский support-flow Telegram |
| [bot/bot_agent_chat.py](bot/bot_agent_chat.py) | рабочий контур специалиста Telegram |
| [bot/bot_orders_management.py](bot/bot_orders_management.py) | история и управление заказами |
| [bot/bot_alfabank_ip.py](bot/bot_alfabank_ip.py) | wizard заявки на ИП |
| [bot/max_bot_main.py](bot/max_bot_main.py) | MAX long polling и базовый router |
| [bot/max_handlers.py](bot/max_handlers.py) | регистрация MAX callbacks и fallback |
| [bot/max_registration.py](bot/max_registration.py) | регистрация и профиль MAX |
| [bot/max_catalog.py](bot/max_catalog.py) | MAX-каталог, корзина и checkout |
| [bot/max_client_chat.py](bot/max_client_chat.py) | клиентский чат MAX |
| [bot/max_agent_chat.py](bot/max_agent_chat.py) | кабинет специалиста MAX |
| [bot/max_orders_management.py](bot/max_orders_management.py) | заказы MAX |
| [bot/max_alfabank_ip.py](bot/max_alfabank_ip.py) | заявка Альфа-Банка в MAX |
| [bot/cross_notify.py](bot/cross_notify.py) | единая отправка в Telegram и MAX |
| [bot/notice_center.py](bot/notice_center.py) | реестр временных уведомлений и cleanup |
| [bot/django_crm.py](bot/django_crm.py) | доставка продаж, заявок и верификаций в CRM |
| [bot/ofd_api.py](bot/ofd_api.py) | клиент OFD API |
| [bot/extern_ofd.py](bot/extern_ofd.py) | каталог, резерв и оплата внешних OFD-продуктов |

### Асинхронный слой данных

| Путь | Ответственность |
|---|---|
| [db/connection.py](db/connection.py) | pool, транзакции, reconnect и тестовый SQL-контур |
| [db/users.py](db/users.py) | идентичность Telegram/MAX и профиль |
| [db/clients.py](db/clients.py) | поиск, merge, verification, компании и ККТ |
| [db/chats.py](db/chats.py) | полный lifecycle обращений, tokens, files, payments |
| [db/orders.py](db/orders.py) | заказы, резервы, оплата, коды, склад, CRM dispatch |
| [db/catalog.py](db/catalog.py) | оборудование и внешний каталог |
| [db/loyalty.py](db/loyalty.py) | ledger и materialized balance лояльности |
| [db/notifications.py](db/notifications.py) | уведомления и дедлайны |
| [db/referrals.py](db/referrals.py) | реферальные коды и активации |
| [db/events.py](db/events.py) | продуктовые события и агрегация |
| [db/alfabank_ip.py](db/alfabank_ip.py) | состояние банковских заявок |

### FastAPI, WebApp и телефония

| Путь | Ответственность |
|---|---|
| [webapp/main.py](webapp/main.py) | lifespan, middleware, rate limit, reconciliation loop |
| [webapp/api.py](webapp/api.py) | чат, файлы, access tokens и платежные API |
| [webapp/utils.py](webapp/utils.py) | идемпотентная финализация оплаты |
| [webapp/asterisk_bridge.py](webapp/asterisk_bridge.py) | async bridge между WebApp-чатом и blocking call module |
| [webapp/python_asterisk_module/](webapp/python_asterisk_module/) | ARI client, controller, schemas, service и repository contract |
| [webapp/python_asterisk_module/ari_client.py](webapp/python_asterisk_module/ari_client.py) | WebSocket ARI и state machine двух ног |
| [webapp/python_asterisk_module/service.py](webapp/python_asterisk_module/service.py) | валидация абонентов и журналирование сессии |

### Отдельный банковский SDK

| Путь | Ответственность |
|---|---|
| [alfabank_partner_api/README.md](alfabank_partner_api/README.md) | документация SDK |
| [alfabank_partner_api/src/alfabank_partner_api/client.py](alfabank_partner_api/src/alfabank_partner_api/client.py) | синхронный клиент |
| [alfabank_partner_api/src/alfabank_partner_api/async_client.py](alfabank_partner_api/src/alfabank_partner_api/async_client.py) | асинхронный клиент |
| [alfabank_partner_api/src/alfabank_partner_api/transport.py](alfabank_partner_api/src/alfabank_partner_api/transport.py) | HTTP, retry, errors, logging masking |
| [alfabank_partner_api/tests/](alfabank_partner_api/tests/) | unit и opt-in integration tests |

## Ключевые backend-решения

### 1. Единая идентичность клиента в Telegram, MAX, CRM и WebApp

Главный ключ системы — внутренний 'users.id'. Telegram ID и MAX ID являются внешними идентификаторами каналов, но не создают отдельные бизнес-сущности.

[db/users.py](db/users.py) и [db/clients.py](db/clients.py) решают несколько сложных сценариев:

- поиск пользователя по внутреннему, Telegram- или MAX-идентификатору;
- создание либо обновление профиля без дублирования;
- связывание заранее внесённого CRM-клиента с новым пользователем бота;
- merge по подтверждённым данным;
- синхронизация флага верификации;
- независимое удаление Telegram/MAX identity;
- полное удаление профиля по пользовательскому запросу;
- общая навигационная snapshot-модель для разных интерфейсов;
- автоматическое создание или связывание компании по данным клиента.

Благодаря этому клиент не распадается на «Telegram-клиента», «MAX-клиента» и «запись CRM».

### 2. Клиенты, контакты, компании и ККТ

CRM разделяет понятия:

- **user/contact** — человек и его каналы связи;
- **company** — юридическая или бизнес-сущность;
- **company_contacts** — связь many-to-many между людьми и компаниями;
- **client_kkt** — единица ККТ, которая может быть связана напрямую с клиентом либо с компанией.

Карточка клиента — агрегат, а не чтение одной таблицы. Она собирает:

- профиль и источник добавления;
- партнёрское и реферальное происхождение;
- прямые ККТ клиента;
- ККТ связанных компаний;
- заказы и права на их редактирование;
- историю обращений;
- custom fields и заметки;
- компании клиента;
- баланс, уровень, рейтинг и операции лояльности;
- заявку Альфа-Банка;
- сведения о специалисте-владельце.

Особенно важна логика объединения ККТ: кассы компании показываются в карточке контакта только при корректной связи с компанией и согласованном ИНН. Это защищает от случайного раскрытия оборудования другой организации.

Карточка компании содержит:

- реквизиты;
- несколько контактов;
- несколько ККТ;
- историю заказов;
- владельца/партнёра;
- отдельные CRUD-операции для контактов и касс.

Для партнёров те же сущности используются через объектный scope: запросы дополнительно ограничиваются 'added_by_partner_id' и 'source_partner_id'.

### 3. Задачи как отдельный бизнес-процесс

Задачи — не простая таблица TODO. В проекте есть отдельный workflow в [bot_app/task_workflow.py](bot_app/task_workflow.py) и сервисный слой в [bot_app/services/task_service.py](bot_app/services/task_service.py).

#### Источники задач

- 'manual' — ручная постановка;
- 'deal' — ручная CRM-сделка;
- 'order' — заказ из Telegram/MAX или оплата в чате;
- 'renewal' — продление услуги или оборудования.

Система синхронизирует задачи из заказов и событий продления. Для автоматически создаваемых задач используются 'source_type', 'source_id' и 'dedupe_key', чтобы повторная синхронизация не плодила дубликаты.

#### Связи и доступ

Одна задача может относиться к нескольким клиентам через 'task_clients'. Для совместимости сохраняется краткая meta-информация, но каноничная связь вынесена в отдельную таблицу.

Директор видит и назначает все задачи. Специалист видит только назначенные ему задачи и получает доступ к связанным карточкам клиента на время активной работы.

#### Workflow

~~~mermaid
stateDiagram-v2
    [*] --> awaiting_assignment
    awaiting_assignment --> in_progress: директор назначил
    in_progress --> postponed: отложить + комментарий
    postponed --> in_progress: вернуть в работу
    in_progress --> done: исполнитель завершил
~~~

Ключевые правила:

- назначить или переназначить исполнителя может директор;
- назначение сразу переводит задачу в работу;
- завершить задачу может только текущий исполнитель;
- отложение требует дату напоминания и обязательный комментарий;
- перенос состояния журналируется;
- завершение ряда задач требует подтверждения, что связанные карточки клиентов/компаний проверены;
- история действий хранится отдельно;
- просрочка вычисляется по deadline, а не записывается вручную;
- список имеет фильтры по статусу, исполнителю, источнику, приоритету, клиенту, поиску и периоду;
- статистика строится с учётом тех же RBAC-фильтров;
- текущая выборка экспортируется в оформленный Excel.

### 4. Омниканальная поддержка и жизненный цикл чата

Поддержка связывает Telegram, MAX, WebApp и Django CRM через общие таблицы 'support_chat_*'.

#### Создание и распределение

Создание обращения идемпотентно: для клиента и профессии повторно используется уже открытый pending/active чат. Это защищает от двойного нажатия и повторной доставки update.

Специалисты распределяются с учётом:

- профессии;
- активности;
- подтверждённого телефона;
- текущей загрузки;
- ранее отказавшихся специалистов.

Принятие чата выполняется условным UPDATE: только один специалист может перевести pending-обращение в active. При переназначении строка блокируется через 'FOR UPDATE', чтобы параллельное принятие не было перезаписано.

#### Доставка и состояние

- сообщения имеют sender-role;
- файлы хранят metadata и связь с сообщением;
- WebApp получает сообщения и файлы по chat token;
- read-state хранит последнюю прочитанную входящую запись;
- CRM возвращает unread-state для живых бейджей;
- cross-channel слой уведомляет участника в доступных мессенджерах;
- notice center обновляет или удаляет временные сообщения, не засоряя чат.

#### Защищённый WebApp-доступ

Для открытия WebApp генерируется криптографический token с ролью, actor и TTL. Истёкший token может быть обновлён, а старый хранит ссылку 'replaced_by'. Resolve проходит по ограниченной цепочке замен; окончательно отозванный или слишком старый token не принимается.

#### Закрытие

Обычное закрытие — двустороннее: клиент и специалист выставляют независимые флаги согласия. После второго подтверждения обращение закрывается. Директор может выполнить force-close. Постоянный чат партнёра с директором защищён от закрытия.

[run_chat_cron.py](run_chat_cron.py) находит зависшие запросы и выполняет автозакрытие по правилам таймаута.

### 5. Телефония Asterisk ARI

Телефония — отдельный call-control модуль, а не HTTP-кнопка «позвонить».

~~~mermaid
sequenceDiagram
    participant CRM as CRM/WebApp
    participant ARI as ARI Controller
    participant S as Специалист
    participant C as Клиент

    CRM->>ARI: start_call(chat_id, phones)
    ARI->>S: originate first leg
    S-->>ARI: Channel Up
    ARI->>ARI: grace period
    ARI->>C: originate second leg
    C-->>ARI: Channel Up
    ARI->>ARI: create mixing bridge
    ARI-->>CRM: connected / final status
~~~

Реализация включает:

- REST-клиент Asterisk ARI;
- постоянное WebSocket-подключение к ARI events;
- автоматическое переподключение после разрыва;
- уникальные channel IDs и session IDs;
- отдельное состояние первой и второй телефонной ноги;
- сначала звонок специалисту, затем клиенту;
- grace-период после ответа специалиста: вторая нога не стартует, если первый канал мгновенно завершился;
- mixing bridge только после состояния Up у обеих сторон;
- уничтожение каналов и bridge при cleanup;
- блокировку повторного звонка для одного чата;
- перенос blocking call-control в thread через 'asyncio.to_thread';
- thread-safe доставку статусов обратно в event loop;
- маскирование номеров в логах;
- системные сообщения о звонке внутри support-чата.

ARI causes нормализуются в бизнес-статусы:

- 'busy';
- 'no_answer';
- 'not_accepted';
- 'cancelled';
- 'failed';
- 'finished'.

Короткое случайное соединение не считается полноценным разговором: итог зависит от фактической длительности bridge и минимального времени разговора.

Внутри [webapp/python_asterisk_module/](webapp/python_asterisk_module/) также выделены Pydantic-схемы 'Abonent', 'CallSession', 'CallLog' и абстрактный 'TelephonyRepository'. Поэтому call-control можно подключать к другой БД/ORM, не привязывая его к Django.

### 6. Заказы, склад, платежи и post-payment pipeline

Каталог Telegram и MAX работает не только как меню. Он объединяет:

- встроенные товары;
- оборудование с остатками;
- пул кодов;
- внешние OFD/сервисные продукты;
- динамическое ценообразование;
- корзину;
- чек;
- резервирование до оплаты;
- T-Bank checkout;
- выдачу после подтверждения.

#### Путь оплаты

~~~mermaid
flowchart TB
    CART["Корзина и проверка остатков"]
    RESERVE["Резерв оборудования/кодов"]
    PAYMENT["T-Bank payment"]
    CONFIRM["Идемпотентная финализация"]
    EFFECTS["Склад · коды · CRM · задача · лояльность"]

    CART --> RESERVE
    RESERVE --> PAYMENT
    PAYMENT --> CONFIRM
    CONFIRM --> EFFECTS
~~~

Критичные решения:

- webhook T-Bank проверяется собственной SHA-256 подписью;
- параллельная финализация одного payment ID сериализуется через async lock;
- заказ блокируется 'SELECT ... FOR UPDATE';
- повторный CONFIRMED не выполняет одноразовые эффекты второй раз;
- остаток оборудования списывается только при первой успешной фиксации;
- срок действия зарезервированного кода начинается после оплаты;
- chat payment связывается с созданным order;
- успешная оплата показывается в чате даже после повторного repair-запуска;
- CRM-событие отправляется best-effort и идемпотентно;
- начисление лояльности отделено от основной транзакции, чтобы сбой бонусов не откатил продажу;
- FastAPI reconciliation loop периодически проверяет незавершённые платежи и восстанавливает пропущенные downstream-эффекты;
- каталог умеет откатывать резервы при неуспешном заказе.

Оплата из чата проходит через роль-ограниченный WebApp API. Создать ссылку может специалист или директор; роль payload сверяется с ролью token.

### 7. Лояльность, рефералы, партнёры и финансовый контур

Лояльность реализована как ledger, а не как одно изменяемое поле 'balance'.

Основные сущности:

- 'loyalty_accounts';
- 'loyalty_transactions';
- 'loyalty_balances';
- 'user_loyalty_profile';
- 'partner_financial_profile';
- 'partner_rating_events';
- 'partner_salary_periods';
- 'partner_salary_adjustments';
- 'bonus_product_policies';
- 'rating_tiers'.

Каждая операция содержит направление, сумму, тип события, reference entity, metadata и уникальный 'idempotency_key'. Повторная доставка события не создаёт второе начисление.

Поддерживаются:

- начисления за покупку и заполнение профиля;
- списание и резервирование баллов;
- release/redeem;
- восстановление materialized balance по ledger;
- срок жизни бонусов;
- ручные операции директора;
- двухуровневые реферальные события;
- рейтинг пользователя и партнёра;
- уровни и правила бонусов;
- вознаграждения/штрафы партнёров;
- расчёт зарплатных периодов;
- согласование, массовое подтверждение и отметка выплаты;
- отдельный финансовый кабинет партнёра;
- экспорт и аналитика операций.

Уведомление о начислении отправляется в Telegram и MAX и автоматически удаляется через TTL, чтобы интерфейс бота не превращался в ленту технических событий.

### 8. Каталог, ценообразование и внешние продукты

Система объединяет собственное оборудование и внешние сервисные продукты.

Для внешних категорий хранятся:

- оператор;
- тип/срок;
- внешнее название;
- закупочная цена;
- фиксированная цена;
- процентная наценка;
- active flag;
- время последней синхронизации.

Цена продажи вычисляется по стратегии fixed-price либо markup. Каталог синхронизирует OFD, сервисы, сервисы по заказу и ЭДО, но позволяет директору управлять локальными overrides.

OFD-контур умеет:

- получать прайс;
- группировать продукты по оператору и сроку;
- резервировать внешний заказ;
- подтверждать оплату;
- сохранять полученные коды в order items;
- использовать stub-режим для безопасной разработки.

### 9. Собственный SDK для Альфа-Банк Partner API

В репозитории находится не один вызов 'requests.post', а отдельная библиотека [alfabank_partner_api/](alfabank_partner_api/).

SDK содержит:

- синхронный 'AlfaBankClient';
- асинхронный 'AlfaBankAsyncClient';
- отдельные resources для leads, checks, scoring и dictionaries;
- Pydantic v2 модели запросов и ответов;
- централизованную конфигурацию;
- sync/async transport;
- типизированную иерархию API-ошибок;
- retry network/5xx через tenacity;
- корректную обработку rate limit;
- masking API key и PII в логах;
- TTL cache справочников;
- polling scoring/result;
- unit tests и opt-in integration tests.

В Telegram и MAX поверх SDK реализован многошаговый wizard:

- предварительное заполнение данных из профиля;
- нормализация телефона и ИНН;
- checksum-проверка ИНН;
- исправление отдельных полей;
- определение кода города;
- создание lead;
- сохранение локальной заявки;
- повторная проверка статуса;
- отображение статуса в карточке клиента CRM.

### 10. Уведомления, аналитика и фоновые процессы

Система имеет несколько независимых уровней наблюдаемости.

**Bot events** фиксируют тип события, имя действия, callback, наличие файла и payload. Директорский dashboard строит конверсию, популярные действия, активность и реферальные показатели.

**CRM notifications** создаются для сроков и бизнес-событий, имеют read/dismiss state и выводятся в центре уведомлений и календаре.

**KKT deadlines** отслеживают ФН, ОФД, маркировку, ЭЦП и другие даты. Просрочки и приближающиеся сроки могут порождать уведомления и задачи на продление.

**Background jobs** выполняют:

- проверку pending support requests;
- OFD reminders;
- автопроверку платежа;
- reconciliation платежей;
- автозакрытие чатов;
- cleanup уведомлений;
- обновление внешнего каталога;
- агрегацию bot events.

Логи разделены между Django и FastAPI; FastAPI использует rotating file handler, request timing и health endpoint.

## Модель данных

Основные Django-проекции перечислены в [bot_app/models.py](bot_app/models.py).

| Домен | Ключевые таблицы |
|---|---|
| Идентичность | 'users', 'ui_state', 'user_custom_fields' |
| Компании | 'companies', 'company_contacts' |
| ККТ | 'client_kkt', 'ofd_subscriptions' |
| Продажи | 'orders', 'order_items', 'equipment', 'codes' |
| Поддержка | 'support_chats', 'support_chat_messages', 'support_chat_files' |
| Решения в чате | 'support_chat_offers', 'support_chat_declines' |
| Платежи чата | 'support_chat_payments', 'support_chat_tokens' |
| Команда | 'specialists', 'specialist_promocodes' |
| Задачи | 'tasks', 'task_clients', 'task_history' |
| Рефералы | 'referral_codes', 'referral_activations' |
| Лояльность | 'loyalty_accounts', 'loyalty_transactions', 'loyalty_balances' |
| Рейтинги и зарплаты | 'partner_rating_events', 'partner_salary_periods', 'partner_salary_adjustments' |
| Цены | 'system_settings', 'ext_product_operators', 'ext_product_types', 'ext_products' |
| Аналитика | 'bot_events' |
| Телефония | abonенты, call sessions и call logs через repository contract |
| Банковские заявки | 'alfabank_ip_applications' |

### Связи верхнего уровня

~~~mermaid
erDiagram
    USERS }o--o{ COMPANIES : company_contacts
    USERS ||--o{ CLIENT_KKT : owns
    COMPANIES ||--o{ CLIENT_KKT : owns
    USERS ||--o{ ORDERS : places
    USERS ||--o{ SUPPORT_CHATS : opens
    SPECIALISTS ||--o{ SUPPORT_CHATS : handles
    TASKS }o--o{ USERS : task_clients
~~~

## Роли и контроль доступа

| Роль | Границы |
|---|---|
| Клиент | собственный профиль, заказы, обращения, баллы и WebApp token |
| Специалист | свои чаты, свои задачи и клиенты, доступные через работу |
| Технический специалист | CRM-карточки закреплённых клиентов и операции ККТ |
| Партнёр | только клиенты/компании своего партнёрского scope, чаты и финансы |
| Директор | CRM, команда, transfer/force-close, задачи, аналитика, цены и финансы |

Контроль выполняется на нескольких уровнях:

- session role в Django;
- decorators в [bot_app/permissions.py](bot_app/permissions.py);
- object-level checks клиента, компании и чата;
- SQL scope партнёра;
- conditional UPDATE для конкурентных операций;
- role-bound WebApp tokens;
- Bearer token для bot-to-CRM API;
- CSRF для браузерных POST;
- проверка роли повторно внутри критичного API.

## Надёжность и безопасность

### Идемпотентность

Используется в местах, где одно событие может прийти повторно:

- создание активного support-чата;
- подтверждение оплаты;
- downstream post-payment repair;
- начисление лояльности;
- создание задач из источников;
- повторное отображение сообщения об оплате;
- повторное назначение того же исполнителя.

### Конкурентность

- 'FOR UPDATE' для распределения/переназначения;
- async lock на payment ID;
- условные UPDATE по текущему статусу;
- registry активных телефонных calls;
- thread-safe delivery ARI events;
- раздельные процессы Telegram и MAX;
- DB pool для асинхронного контура.

### Web-защита

Django:

- CSRF middleware;
- HttpOnly session cookie;
- SameSite cookies;
- 8-часовая сессия;
- rate limit попыток входа;
- password hash migration;
- role decorators.

FastAPI:

- rate limit по IP;
- временная блокировка подозрительных клиентов;
- фильтрация известных scanner paths и user agents;
- request timeout для mutation-запросов;
- CORS из окружения;
- token authorization на chat resources;
- проверка webhook signature;
- health endpoint.

### Безопасность публичной версии

Это очищенный публичный снимок проекта без рабочей Git-истории, production env-файлов, runtime-платежей и пользовательских данных. Для локального запуска используется только шаблон [inf.env.example](inf.env.example); реальные значения должны храниться вне Git.

Перед production-развёртыванием необходимо использовать демонстрационную или отдельную базу, отключить debug, ограничить allowed hosts и CORS, включить HTTPS и secure cookies, а также выполнить `python manage.py check --deploy`. Дополнительные правила описаны в [SECURITY.md](SECURITY.md).

## Основные маршруты

| Маршрут | Назначение |
|---|---|
| '/' | директорский dashboard |
| '/crm/' | клиенты |
| '/crm/companies/' | компании |
| '/crm/chats/' | обращения |
| '/crm/orders/' | заказы и сделки |
| '/crm/tasks/' | задачи |
| '/analytics/' | продуктовая аналитика |
| '/notifications/' | уведомления |
| '/notifications/calendar/' | календарь |
| '/specialist/' | рабочий кабинет специалиста |
| '/partner/clients/' | клиенты партнёра |
| '/partner/companies/' | компании партнёра |
| '/partner/chats/' | обращения партнёра |
| '/partner/analytics/' | аналитика партнёра |
| '/loyalty/dashboard/' | финансовый dashboard |
| '/loyalty/policies/' | правила лояльности |
| '/loyalty/partners/' | финансы партнёров |
| '/loyalty/journal/' | журнал операций |
| '/loyalty/manual-operations/' | ручные операции |
| '/loyalty/analytics/' | аналитика лояльности |
| '/my-finance/' | кабинет партнёра |
| '/settings/' | системные настройки |
| '/settings/external-products/' | внешний каталог и цены |
| '/api/sales/' | приём продажи от бота |
| '/api/applications/' | приём заявки |
| '/api/verifications/' | приём верификации |

Полная карта URL находится в [bot_app/urls.py](bot_app/urls.py).

## Локальный запуск

### 1. Python-окружение

~~~bash
git clone https://github.com/Shira-Artem/bot_crm-public.git
cd bot_crm-public

python -m venv .venv
~~~

Windows:

~~~powershell
.venv\Scripts\activate
pip install -r requirements.txt
~~~

Linux/macOS:

~~~bash
source .venv/bin/activate
pip install -r requirements.txt
~~~

Для локального SDK Альфа-Банка:

~~~bash
pip install -e ./alfabank_partner_api
~~~

### 2. Переменные окружения

Скопируйте `inf.env.example` в локальный `inf.env`. Реальные значения не должны попадать в Git.

~~~env
DJANGO_SECRET_KEY=replace-with-long-random-value
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1

MYSQL_DB=bot_db
MYSQL_USER=bot_crm
MYSQL_PASSWORD=replace-me
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_POOL_SIZE=10

BOT_TOKEN=telegram-token
MAX_BOT_TOKEN=max-token
WEBAPP_URL=http://127.0.0.1:8443
BOT_CRM_API_TOKEN=internal-bearer-token

TINKOFF_TERMINAL_KEY=terminal-key
TINKOFF_PASSWORD=terminal-password
~~~

Для телефонии дополнительно используются 'ASTERISK_ENABLED', 'ARI_BASE_URL', 'ARI_WS_URL', 'ARI_USERNAME', 'ARI_PASSWORD', 'ARI_APP_NAME', 'ARI_TRUNK_ENDPOINT' и 'ARI_DEFAULT_CALLERID'.

Для Альфа-Банка — переменные с префиксом 'ALFABANK_'.

### 3. База данных

Схема проекта развивается SQL-миграциями из [migrations/](migrations/). Применяйте их в порядке нумерации к резервной копии или чистой локальной базе.

> Большинство Django-моделей имеет 'managed = False'. Обычная команда 'python manage.py migrate' не создаёт доменные таблицы проекта.

В коде присутствуют 'ensure_*' функции для совместимости и самовосстановления старых установок. Новые изменения схемы должны оформляться отдельной SQL-миграцией; runtime DDL не должен становиться основным способом управления production-схемой.

### 4. Запуск

Django CRM:

~~~bash
python manage.py runserver
~~~

Telegram:

~~~bash
python run_bot.py
~~~

MAX:

~~~bash
python run_max_bot.py
~~~

FastAPI WebApp:

~~~bash
uvicorn webapp.main:app --host 0.0.0.0 --port 8443
~~~

Chat cron:

~~~bash
python run_chat_cron.py
~~~

## Тестирование

Основные тестовые точки:

- [bot_app/test_task_workflow.py](bot_app/test_task_workflow.py) — workflow задач;
- [test_finance_crm_module.py](test_finance_crm_module.py) — финансовый контур;
- [test_finance_simple.py](test_finance_simple.py) — упрощённая проверка финансов;
- [alfabank_partner_api/tests/unit/](alfabank_partner_api/tests/unit/) — unit suite SDK;
- [alfabank_partner_api/tests/integration/](alfabank_partner_api/tests/integration/) — opt-in интеграционные тесты.

Пример:

~~~bash
python manage.py check
python manage.py test bot_app.test_task_workflow

PYTHONPATH=alfabank_partner_api/src \
pytest alfabank_partner_api/tests/unit -q
~~~

Интеграционные банковские тесты запускаются только в отдельном безопасном окружении с разрешающим флагом и demo credentials.

## Правила развития проекта

Чтобы не потерять управляемость большой системы:

1. **Искать домен через карту выше.** URL/view — это вход, бизнес-операция должна находиться в service/db слое.
2. **Не создавать второй источник истины.** Telegram и MAX должны использовать общий внутренний user ID и общие таблицы.
3. **Сохранять идемпотентность.** Webhook, polling и повторные callbacks неизбежны.
4. **Не смешивать payment confirmation и побочные эффекты.** Критичная фиксация оплаты должна завершаться отдельно от best-effort уведомлений.
5. **Изменять schema только миграцией.** 'ensure_*' оставлять как слой совместимости.
6. **Добавлять object-level access checks.** Ролевого decorator недостаточно для клиента, компании, задачи или чата.
7. **Учитывать оба мессенджера.** Новое пользовательское событие должно иметь Telegram/MAX parity либо явно задокументированное ограничение.
8. **Не логировать PII и secrets.** Телефоны маскировать, tokens не выводить.
9. **Фиксировать state transition.** Для задач, чатов, платежей и звонков статус меняется только через каноничный workflow.
10. **Добавлять repair path.** Внешняя система может подтвердить операцию после timeout или потери ответа.

## Технологический стек

| Область | Технологии |
|---|---|
| Backend | Python, Django 4.2.9, FastAPI |
| ASGI/WSGI | Uvicorn, Gunicorn |
| База | MySQL, PyMySQL, mysql-connector-python aio |
| Bots | python-telegram-bot 22, MAX Platform API |
| Async | asyncio, aiohttp, httpx |
| Validation | Pydantic v2 |
| Frontend | Django Templates, Jinja2, HTML, CSS, JavaScript, AJAX |
| Payments | T-Bank API |
| Telephony | Asterisk ARI, REST, WebSocket, PJSIP |
| Fiscal/services | OFD API, external service catalogs |
| Banking | Alfa-Bank Partner API, собственный sync/async SDK |
| Files/reports | python-multipart, openpyxl, qrcode |
| Resilience | transactions, idempotency keys, locks, tenacity, reconciliation |
| Security | CSRF, sessions, Bearer tokens, token TTL/rotation, cryptography |
| Observability | structured logging, rotating files, bot events, health endpoint |

## Автор

**Широчин Артём Викторович**

Проект демонстрирует проектирование и разработку сложной бизнес-системы: омниканальную идентичность, транзакционный backend, асинхронные боты, CRM, телефонию, платежи, финансовый ledger, внешний SDK и интеграции с несколькими API.

---

Репозиторий находится в активной разработке. Этот README одновременно служит портфолио проекта и технической картой для дальнейшей работы команды.
