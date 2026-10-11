-- ==============================================================================
-- МИГРАЦИЯ: Системные настройки директора
-- Дата: 2026-03-20
-- Описание: Таблица ключ-значение для бизнес-настроек, редактируемых через bot_app
-- ==============================================================================

USE bot_db;

CREATE TABLE IF NOT EXISTS `system_settings` (
    `name` VARCHAR(100) PRIMARY KEY,
    `value_type` ENUM('number', 'text') NOT NULL,
    `char_value` VARCHAR(1000) DEFAULT NULL,
    `number_value` DECIMAL(12,4) DEFAULT NULL,
    `description` VARCHAR(255) DEFAULT NULL,
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    `updated_by` BIGINT DEFAULT NULL,

    INDEX `idx_system_settings_value_type` (`value_type`),
    INDEX `idx_system_settings_updated_by` (`updated_by`),

    CONSTRAINT `fk_system_settings_updated_by` FOREIGN KEY (`updated_by`)
        REFERENCES `specialists` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `system_settings` (`name`, `value_type`, `number_value`, `description`) VALUES
    ('EXTERNAL_OFD_MARKUP_PCT', 'number', 740, 'Глобальная наценка для внешних товаров ОФД'),
    ('EXTERNAL_SERVICES_MARKUP_PCT', 'number', 740, 'Глобальная наценка для внешних сервисов'),
    ('EXTERNAL_EDO_MARKUP_PCT', 'number', 740, 'Глобальная наценка для внешних ЭДО')
ON DUPLICATE KEY UPDATE
    `value_type` = VALUES(`value_type`),
    `description` = VALUES(`description`);

SELECT 'Миграция 009: системные настройки директора — применена!' AS result;
