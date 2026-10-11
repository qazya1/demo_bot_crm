-- ====================================================================
-- Тестовый скрипт: проверка создания задач из всех источников
-- Запуск: mysql -u root -p bot_db < test_tasks_sources.sql
-- ====================================================================

-- Убедимся что таблица tasks существует
CREATE TABLE IF NOT EXISTS tasks (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    title       VARCHAR(500)   NOT NULL,
    description TEXT           NULL,
    source_type VARCHAR(32)    NOT NULL DEFAULT 'manual',
    source_id   BIGINT         NULL,
    created_by  BIGINT         NOT NULL,
    assignee_id BIGINT         NULL,
    deadline    DATETIME       NULL,
    priority    VARCHAR(16)    NOT NULL DEFAULT 'medium',
    status      VARCHAR(32)    NOT NULL DEFAULT 'new',
    result_comment TEXT        NULL,
    created_at  DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_tasks_assignee  (assignee_id),
    INDEX idx_tasks_status    (status),
    INDEX idx_tasks_deadline  (deadline),
    INDEX idx_tasks_source    (source_type, source_id),
    INDEX idx_tasks_created_by(created_by)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ====================================================================
-- Находим ID директора и специалиста для тестов
-- ====================================================================
SET @director_id = (SELECT id FROM specialists WHERE profession = 2 LIMIT 1);
SET @specialist_id = (SELECT id FROM specialists WHERE profession != 2 AND is_active = 1 LIMIT 1);

SELECT CONCAT('Директор ID: ', COALESCE(@director_id, 'НЕ НАЙДЕН')) AS info;
SELECT CONCAT('Специалист ID: ', COALESCE(@specialist_id, 'НЕ НАЙДЕН')) AS info;

-- Если нет специалиста — используем директора как исполнителя (для теста)
SET @assignee = COALESCE(@specialist_id, @director_id);

-- ====================================================================
-- 1. Задача из СДЕЛКИ (source_type = 'deal')
-- ====================================================================
-- Находим последнюю сделку
SET @deal_id = (SELECT id FROM orders ORDER BY id DESC LIMIT 1);
SELECT CONCAT('Последняя сделка ID: ', COALESCE(@deal_id, 'НЕТ СДЕЛОК')) AS info;

INSERT INTO tasks (title, description, source_type, source_id, created_by, assignee_id, deadline, priority, status)
VALUES (
    CONCAT('Обработать сделку #', COALESCE(@deal_id, 0)),
    CONCAT('Автотест: задача из сделки. Проверить оплату и подготовить документы для сделки #', COALESCE(@deal_id, 0)),
    'deal',
    @deal_id,
    @director_id,
    @assignee,
    DATE_ADD(NOW(), INTERVAL 2 HOUR),
    'high',
    'new'
);
SELECT CONCAT('Создана задача из сделки, ID: ', LAST_INSERT_ID()) AS result;

-- ====================================================================
-- 2. Задача из ПРОДЛЕНИЯ (source_type = 'renewal')
-- ====================================================================
-- Находим ККТ с ближайшим сроком ОФД
SET @kkt_id = (SELECT id FROM client_kkt WHERE ofd_expires_at IS NOT NULL ORDER BY ofd_expires_at ASC LIMIT 1);
SELECT CONCAT('ККТ для продления ID: ', COALESCE(@kkt_id, 'НЕТ ККТ')) AS info;

-- Если нет ККТ — создаём тестовую
INSERT INTO client_kkt (client_id, kkt_slot, address, ofd_expires_at, fn_expires_at, created_at, updated_at)
SELECT
    (SELECT id FROM users LIMIT 1),
    'TEST-KKT-001',
    'Тестовый адрес для проверки продлений',
    DATE_ADD(NOW(), INTERVAL 15 DAY),
    DATE_ADD(NOW(), INTERVAL 30 DAY),
    NOW(), NOW()
FROM DUAL
WHERE @kkt_id IS NULL;
SET @kkt_id = COALESCE(@kkt_id, LAST_INSERT_ID());

INSERT INTO tasks (title, description, source_type, source_id, created_by, assignee_id, deadline, priority, status)
VALUES (
    CONCAT('Продление ОФД для ККТ #', @kkt_id),
    CONCAT('Автотест: задача из продления. Срок ОФД заканчивается через 15 дней. Связаться с клиентом для продления ОФД. ККТ #', @kkt_id),
    'renewal',
    @kkt_id,
    @director_id,
    @assignee,
    DATE_ADD(NOW(), INTERVAL 1 DAY),
    'medium',
    'new'
);
SELECT CONCAT('Создана задача из продления, ID: ', LAST_INSERT_ID()) AS result;

-- ====================================================================
-- 3. Задача из НОВОГО ЗАКАЗА (source_type = 'order')
-- ====================================================================
-- Создаём тестовый заказ если нужно
SET @order_id = (SELECT id FROM orders WHERE status = 'INIT' ORDER BY id DESC LIMIT 1);

-- Если нет — находим любой заказ
SET @order_id = COALESCE(@order_id, (SELECT id FROM orders ORDER BY id DESC LIMIT 1));
SELECT CONCAT('Заказ для задачи ID: ', COALESCE(@order_id, 'НЕТ ЗАКАЗОВ')) AS info;

INSERT INTO tasks (title, description, source_type, source_id, created_by, assignee_id, deadline, priority, status)
VALUES (
    CONCAT('Новый заказ #', COALESCE(@order_id, 0), ' — подготовить к отправке'),
    CONCAT('Автотест: задача из нового заказа. Проверить наличие товара, подготовить отправку. Заказ #', COALESCE(@order_id, 0)),
    'order',
    @order_id,
    @director_id,
    @assignee,
    DATE_ADD(NOW(), INTERVAL 4 HOUR),
    'high',
    'in_progress'
);
SELECT CONCAT('Создана задача из заказа, ID: ', LAST_INSERT_ID()) AS result;

-- ====================================================================
-- 4. Ручная задача от директора (source_type = 'manual')
-- ====================================================================
INSERT INTO tasks (title, description, source_type, source_id, created_by, assignee_id, deadline, priority, status)
VALUES (
    'Подготовить отчёт за неделю',
    'Автотест: ручная задача от директора. Собрать данные по продажам за текущую неделю и подготовить сводный отчёт.',
    'manual',
    NULL,
    @director_id,
    @assignee,
    DATE_ADD(NOW(), INTERVAL 8 HOUR),
    'low',
    'new'
);
SELECT CONCAT('Создана ручная задача, ID: ', LAST_INSERT_ID()) AS result;

-- ====================================================================
-- 5. Задача с просроченным дедлайном (для теста красной подсветки)
-- ====================================================================
INSERT INTO tasks (title, description, source_type, source_id, created_by, assignee_id, deadline, priority, status)
VALUES (
    'ПРОСРОЧЕННАЯ: Срочно проверить оплату',
    'Автотест: просроченная задача. Дедлайн уже прошёл.',
    'deal',
    @deal_id,
    @director_id,
    @assignee,
    DATE_SUB(NOW(), INTERVAL 2 HOUR),
    'high',
    'in_progress'
);
SELECT CONCAT('Создана просроченная задача, ID: ', LAST_INSERT_ID()) AS result;

-- ====================================================================
-- 6. Выполненная задача (для теста счётчика)
-- ====================================================================
INSERT INTO tasks (title, description, source_type, source_id, created_by, assignee_id, deadline, priority, status, result_comment)
VALUES (
    'Выполнено: Настройка ККТ клиенту',
    'Автотест: выполненная задача.',
    'order',
    @order_id,
    @director_id,
    @assignee,
    DATE_SUB(NOW(), INTERVAL 1 DAY),
    'medium',
    'done',
    'Всё сделано, ККТ настроена и работает.'
);
SELECT CONCAT('Создана выполненная задача, ID: ', LAST_INSERT_ID()) AS result;

-- ====================================================================
-- Итоговая проверка
-- ====================================================================
SELECT '===== ИТОГО ЗАДАЧ В СИСТЕМЕ =====' AS header;
SELECT
    COUNT(*) AS total_tasks,
    SUM(status = 'new') AS new_tasks,
    SUM(status = 'in_progress') AS in_progress,
    SUM(status = 'waiting') AS waiting,
    SUM(status = 'done') AS done_tasks,
    SUM(deadline < NOW() AND status NOT IN ('done')) AS overdue
FROM tasks;

SELECT '===== ВСЕ ЗАДАЧИ =====' AS header;
SELECT
    t.id,
    t.title,
    t.source_type,
    t.source_id,
    t.status,
    t.priority,
    t.deadline,
    t.assignee_id,
    s.name AS assignee_name,
    CASE WHEN t.deadline < NOW() AND t.status != 'done' THEN 'ДА' ELSE 'нет' END AS is_overdue
FROM tasks t
LEFT JOIN specialists s ON s.id = t.assignee_id
ORDER BY t.id DESC;

SELECT '===== ПРОВЕРКА ИСТОЧНИКОВ =====' AS header;
SELECT source_type, COUNT(*) AS cnt FROM tasks GROUP BY source_type;
