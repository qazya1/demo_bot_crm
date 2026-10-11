-- =============================================================
-- Миграция 012: Добавление колонки we_added в таблицу users
-- Дата: 2026-03-21
-- Описание: Флаг "Наш клиент" — указывает, что клиент
--           был добавлен нами (через CRM / специалистом).
-- =============================================================

USE bot_db;

-- Добавляем we_added (TINYINT(1), по умолчанию 0)
-- Используем PROCEDURE + INFORMATION_SCHEMA для совместимости с MySQL 5.7/8.0
DROP PROCEDURE IF EXISTS _add_we_added;
DELIMITER $$
CREATE PROCEDURE _add_we_added()
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'users'
          AND COLUMN_NAME = 'we_added'
    ) THEN
        ALTER TABLE `users` ADD COLUMN `we_added` TINYINT(1) NOT NULL DEFAULT 0
            COMMENT 'Наш клиент (добавлен через CRM)';
    END IF;
END$$
DELIMITER ;

CALL _add_we_added();
DROP PROCEDURE IF EXISTS _add_we_added;

SELECT 'Миграция 012: we_added — применена!' AS result;
