-- =============================================================
-- Миграция 013: Добавление колонок added_by_specialist_id
-- Дата: 2026-03-21
-- Описание: Хранит ID специалиста/директора, который создал
--           клиента или компанию через CRM.
-- =============================================================

USE bot_db;

DROP PROCEDURE IF EXISTS _add_added_by_specialist_columns;
DELIMITER $$
CREATE PROCEDURE _add_added_by_specialist_columns()
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'users'
          AND COLUMN_NAME = 'added_by_specialist_id'
    ) THEN
        ALTER TABLE `users`
            ADD COLUMN `added_by_specialist_id` INT NULL DEFAULT NULL
            COMMENT 'ID специалиста/директора, создавшего клиента через CRM';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'companies'
          AND COLUMN_NAME = 'added_by_specialist_id'
    ) THEN
        ALTER TABLE `companies`
            ADD COLUMN `added_by_specialist_id` INT NULL DEFAULT NULL
            COMMENT 'ID специалиста/директора, создавшего компанию через CRM';
    END IF;
END$$
DELIMITER ;

CALL _add_added_by_specialist_columns();
DROP PROCEDURE IF EXISTS _add_added_by_specialist_columns;

SELECT 'Миграция 013: added_by_specialist_id — применена!' AS result;
