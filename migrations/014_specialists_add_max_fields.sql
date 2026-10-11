-- Фаза: Добавление поддержки MAX Messenger в таблицу specialists
-- Поля max_user_id и max_username для кросс-платформенной работы.
-- Запуск: mysql -u root -p bot_db < migrations/014_specialists_add_max_fields.sql

-- max_user_id (если ещё нет)
SET @col_exists = (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE()
    AND table_name = 'specialists'
    AND column_name = 'max_user_id'
);
SET @sql = IF(@col_exists = 0,
  'ALTER TABLE specialists ADD COLUMN max_user_id BIGINT NULL DEFAULT NULL',
  'SELECT 1');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- max_username
SET @col_exists2 = (
  SELECT COUNT(*) FROM information_schema.columns
  WHERE table_schema = DATABASE()
    AND table_name = 'specialists'
    AND column_name = 'max_username'
);
SET @sql2 = IF(@col_exists2 = 0,
  'ALTER TABLE specialists ADD COLUMN max_username VARCHAR(64) NULL DEFAULT NULL',
  'SELECT 1');
PREPARE stmt2 FROM @sql2; EXECUTE stmt2; DEALLOCATE PREPARE stmt2;
