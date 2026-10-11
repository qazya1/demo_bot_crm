-- Добавление колонок accepted_at и closed_at в support_chats
-- Эти колонки нужны для отслеживания SLA и метрик специалистов

-- accepted_at — время принятия заявки специалистом
SET @col_exists = (SELECT COUNT(*)
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'support_chats'
      AND column_name = 'accepted_at');

SET @sql = IF(@col_exists = 0,
    'ALTER TABLE support_chats ADD COLUMN accepted_at TIMESTAMP NULL DEFAULT NULL AFTER updated_at',
    'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- closed_at — время закрытия чата
SET @col_exists = (SELECT COUNT(*)
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'support_chats'
      AND column_name = 'closed_at');

SET @sql = IF(@col_exists = 0,
    'ALTER TABLE support_chats ADD COLUMN closed_at TIMESTAMP NULL DEFAULT NULL AFTER accepted_at',
    'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
