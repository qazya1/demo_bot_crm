-- Ручной сценарий для проверки задач в CRM.
-- 1) Полностью очищает таблицу tasks.
-- 2) Добавляет тестовые задачи от директора в статусе awaiting_assignment.
-- 3) Все задачи без assignee_id (должны отображаться у директора как "Ожидают").

START TRANSACTION;

SET @director_id := (
    SELECT id
    FROM specialists
    WHERE is_director = 1 OR role = 'director'
    ORDER BY id
    LIMIT 1
);

-- Если директор не найден, скрипт упадёт на NOT NULL ниже — это ожидаемое поведение.
DELETE FROM task_clients;
DELETE FROM tasks;

INSERT INTO tasks
    (title, description, source_type, source_id, dedupe_key, meta_json,
     director_id, created_by, assignee_id, client_id, deadline, priority, status)
VALUES
    (
        'Тест #1: первичный звонок клиенту',
        'Проверка отображения карточки задачи у специалиста после назначения директором.',
        'manual',
        NULL,
        CONCAT('manual-check-1-', UNIX_TIMESTAMP()),
        JSON_OBJECT('manual_check', true, 'scenario', 'assignment_flow'),
        @director_id,
        @director_id,
        NULL,
        NULL,
        DATE_ADD(NOW(), INTERVAL 1 DAY),
        'medium',
        'awaiting_assignment'
    ),
    (
        'Тест #2: уточнить реквизиты',
        'Проверка привязки и видимости задачи в списке "Ожидают".',
        'manual',
        NULL,
        CONCAT('manual-check-2-', UNIX_TIMESTAMP()),
        JSON_OBJECT('manual_check', true, 'scenario', 'awaiting_list'),
        @director_id,
        @director_id,
        NULL,
        NULL,
        DATE_ADD(NOW(), INTERVAL 2 DAY),
        'high',
        'awaiting_assignment'
    ),
    (
        'Тест #3: подготовить КП',
        'Проверка полного цикла: назначение -> выполнение.',
        'manual',
        NULL,
        CONCAT('manual-check-3-', UNIX_TIMESTAMP()),
        JSON_OBJECT('manual_check', true, 'scenario', 'full_cycle'),
        @director_id,
        @director_id,
        NULL,
        NULL,
        DATE_ADD(NOW(), INTERVAL 3 DAY),
        'low',
        'awaiting_assignment'
    );

COMMIT;
