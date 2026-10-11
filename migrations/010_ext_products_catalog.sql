-- ==============================================================================
-- МИГРАЦИЯ: Внешний каталог продуктов в БД
-- Дата: 2026-03-20
-- Описание: Справочники операторов/типов + таблица ext_products вместо ext_price_overrides
-- ==============================================================================

USE bot_db;

CREATE TABLE IF NOT EXISTS `ext_product_operators` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `category` ENUM('ofd', 'service', 'services_for_orders', 'edo') NOT NULL,
    `operator_id` INT NOT NULL,
    `name` VARCHAR(255) NOT NULL,
    `is_active` TINYINT(1) NOT NULL DEFAULT 1,
    `sort_order` INT NOT NULL DEFAULT 0,
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uq_ext_product_operator` (`category`, `operator_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `ext_product_types` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `type_id` INT NOT NULL,
    `name` VARCHAR(255) NOT NULL,
    `months_number` INT DEFAULT NULL,
    `is_active` TINYINT(1) NOT NULL DEFAULT 1,
    `sort_order` INT NOT NULL DEFAULT 0,
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uq_ext_product_type` (`type_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `ext_products` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `category` ENUM('ofd', 'service', 'services_for_orders', 'edo') NOT NULL,
    `operator_id` INT NOT NULL,
    `type_id` INT NOT NULL,
    `external_name` VARCHAR(255) DEFAULT NULL,
    `source_price` INT DEFAULT NULL,
    `price_fixed` INT DEFAULT NULL,
    `markup_pct` DECIMAL(8,2) DEFAULT NULL,
    `is_active` TINYINT(1) NOT NULL DEFAULT 1,
    `comment` VARCHAR(255) DEFAULT NULL,
    `last_seen_at` DATETIME DEFAULT NULL,
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uq_ext_product` (`category`, `operator_id`, `type_id`),
    KEY `idx_ext_products_active` (`category`, `is_active`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT INTO `ext_product_operators` (`category`, `operator_id`, `name`, `sort_order`) VALUES
    ('ofd', 1, 'Такском (TAXCOM)', 1),
    ('ofd', 2, 'Учет Марок Платформа ОФД. Выбытие товаров', 2),
    ('ofd', 3, 'ОФД Ярус (ОФД-Я)', 3),
    ('ofd', 4, 'Первый ОФД (1-ofd)', 4),
    ('ofd', 5, 'ПЕТЕР-СЕРВИС (OFD.RU)', 5),
    ('ofd', 6, 'ОФД Яндекс', 6),
    ('ofd', 8, 'Платформа ОФД (Эвотор)', 7),
    ('ofd', 9, 'Тензор (СБИС)', 8),
    ('ofd', 18, 'Калуга Астрал ОФД', 9),
    ('service', 12, 'ПО «Дримкас Ключ». Тариф «Базовый»', 1),
    ('service', 24, 'ПО «Дримкас Учёт». Лицензия', 2),
    ('service', 25, 'ПО АТОЛ Sigma (тариф "СТАРТ")', 3),
    ('service', 29, 'Лицензия КриптоПро CSP 5.0', 4),
    ('service', 30, 'ПО АТОЛ Sigma (тариф "РАЗВИТИЕ")', 5),
    ('service', 32, 'Лицензия КриптоПро CSP 5.0 (на один ПК)', 6),
    ('service', 33, 'ПО «Дримкас Старт». Лицензия', 7),
    ('service', 36, 'ПО АТОЛ Sigma (тариф "БИЗНЕС")', 8),
    ('service', 41, 'ПО Frontol 6 тариф "Базовый" (S774)', 9),
    ('service', 73, 'ПО Frontol Mark Unit', 10),
    ('service', 76, 'ПО Frontol 6 тариф "Полный" (S775)', 11),
    ('services_for_orders', 47, 'АТОЛ Connect. ИТС на 1 год', 1),
    ('services_for_orders', 48, 'ПО Штрих-М Кассир 5 (12 мес)', 2),
    ('services_for_orders', 74, 'Подписка 365 дней Штрих-М', 3),
    ('services_for_orders', 77, 'Подписка для СМАРТПОС-Ф', 4),
    ('services_for_orders', 37, 'Подписка Посцентр/Ритейл', 5),
    ('services_for_orders', 75, 'Подписка РР Электро', 6),
    ('services_for_orders', 44, 'Эвотор Модернизация СН СТ (горячая замена)', 7),
    ('edo', 17, 'Платформа, отчетность ФНС', 1),
    ('edo', 23, 'ЭДО Платформа ЭДО (100 документов)', 2)
ON DUPLICATE KEY UPDATE `name` = VALUES(`name`), `sort_order` = VALUES(`sort_order`);

INSERT INTO `ext_product_types` (`type_id`, `name`, `months_number`, `sort_order`) VALUES
    (4, '12 месяцев', 12, 1),
    (5, '15 месяцев', 15, 2),
    (6, '36 месяцев', 36, 3),
    (9, 'Разовый код', NULL, 4),
    (10, '13 месяцев', 13, 5),
    (12, 'Универсальный код', NULL, 6),
    (11, 'Бессрочная лицензия', NULL, 7)
ON DUPLICATE KEY UPDATE `name` = VALUES(`name`), `months_number` = VALUES(`months_number`), `sort_order` = VALUES(`sort_order`);

INSERT INTO `ext_products` (`category`, `operator_id`, `type_id`, `price_fixed`, `markup_pct`, `is_active`, `comment`, `updated_at`)
SELECT e.`category`, e.`operator_id`, e.`type_id`, e.`price_fixed`, e.`markup_pct`, e.`is_active`, e.`comment`, e.`updated_at`
FROM `ext_price_overrides` e
LEFT JOIN `ext_products` p
  ON p.`category` = e.`category` AND p.`operator_id` = e.`operator_id` AND p.`type_id` = e.`type_id`
WHERE p.`id` IS NULL;

SELECT 'Миграция 010: внешний каталог продуктов — применена!' AS result;
