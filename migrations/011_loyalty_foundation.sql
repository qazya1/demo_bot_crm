-- =============================================================
-- Миграция 011: Фундамент финансовой модели лояльности
-- Дата: 2026-03-21
-- Описание: Создание таблиц клиентской лояльности, партнёрской
--           компенсации, нормативных справочников, расширение
--           существующих таблиц, начальные system_settings.
-- =============================================================

USE bot_db;

-- ─────────────────────────────────────────────────────────────
-- A.1  Клиентский контур лояльности
-- ─────────────────────────────────────────────────────────────

-- Единый бонусный счёт (owner_type позволяет расширить на партнёров)
CREATE TABLE IF NOT EXISTS `loyalty_accounts` (
    `id`           BIGINT AUTO_INCREMENT PRIMARY KEY,
    `owner_type`   ENUM('client','partner') NOT NULL DEFAULT 'client',
    `owner_id`     BIGINT NOT NULL COMMENT 'users.id или specialists.id',
    `is_active`    TINYINT(1) NOT NULL DEFAULT 1,
    `created_at`   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uq_loyalty_account_owner` (`owner_type`, `owner_id`),
    KEY `ix_loyalty_accounts_active` (`is_active`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Журнал операций (append-only ledger)
CREATE TABLE IF NOT EXISTS `loyalty_transactions` (
    `id`                          BIGINT AUTO_INCREMENT PRIMARY KEY,
    `account_id`                  BIGINT NOT NULL,
    `direction`                   ENUM('credit','debit') NOT NULL COMMENT 'credit=начисление, debit=списание',
    `amount`                      INT NOT NULL COMMENT 'Всегда положительное, направление определяется direction',
    `event_type`                  VARCHAR(64) NOT NULL COMMENT 'purchase, first_purchase, profile_field, referral, redeem, expiry, manual и др.',
    `idempotency_key`             VARCHAR(128) NOT NULL COMMENT 'Уникальный ключ для идемпотентности',
    `description`                 VARCHAR(255) DEFAULT NULL COMMENT 'Человекочитаемое описание для отображения в боте',
    `rating_delta`                INT NOT NULL DEFAULT 0 COMMENT 'Изменение рейтинга пользователя',
    `reversal_of_transaction_id`  BIGINT DEFAULT NULL COMMENT 'Ссылка на сторнируемую транзакцию',
    `expires_at`                  DATETIME DEFAULT NULL COMMENT 'Дата сгорания баллов (NULL = бессрочно)',
    `is_expired`                  TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'Флаг: баллы уже сгорели',
    `ref_entity_type`             VARCHAR(32) DEFAULT NULL COMMENT 'order, chat_payment, referral и т.д.',
    `ref_entity_id`               BIGINT DEFAULT NULL COMMENT 'ID связанной сущности',
    `meta_json`                   JSON DEFAULT NULL COMMENT 'Произвольные данные',
    `created_at`                  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY `uq_loyalty_tx_idempotency` (`idempotency_key`),
    KEY `ix_loyalty_tx_account` (`account_id`, `created_at`),
    KEY `ix_loyalty_tx_event_type` (`event_type`),
    KEY `ix_loyalty_tx_expires` (`is_expired`, `expires_at`),
    KEY `ix_loyalty_tx_reversal` (`reversal_of_transaction_id`),
    CONSTRAINT `fk_loyalty_tx_account`
        FOREIGN KEY (`account_id`) REFERENCES `loyalty_accounts` (`id`) ON DELETE RESTRICT,
    CONSTRAINT `fk_loyalty_tx_reversal`
        FOREIGN KEY (`reversal_of_transaction_id`) REFERENCES `loyalty_transactions` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Материализованный баланс (обновляется при каждой транзакции)
CREATE TABLE IF NOT EXISTS `loyalty_balances` (
    `account_id`       BIGINT PRIMARY KEY,
    `available_amount`  INT NOT NULL DEFAULT 0,
    `reserved_amount`   INT NOT NULL DEFAULT 0,
    `total_earned`      INT NOT NULL DEFAULT 0 COMMENT 'Всего начислено за всё время',
    `total_spent`       INT NOT NULL DEFAULT 0 COMMENT 'Всего списано за всё время',
    `updated_at`        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT `fk_loyalty_bal_account`
        FOREIGN KEY (`account_id`) REFERENCES `loyalty_accounts` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Профиль лояльности пользователя (рейтинг, уровень)
CREATE TABLE IF NOT EXISTS `user_loyalty_profile` (
    `user_id`          BIGINT PRIMARY KEY COMMENT 'users.id (internal)',
    `rating`           INT NOT NULL DEFAULT 0,
    `tier`             VARCHAR(32) NOT NULL DEFAULT 'bronze',
    `total_orders`     INT NOT NULL DEFAULT 0,
    `total_spent_rub`  DECIMAL(12,2) NOT NULL DEFAULT 0,
    `last_activity_at` DATETIME DEFAULT NULL,
    `created_at`       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY `ix_ulp_rating` (`rating`),
    KEY `ix_ulp_tier` (`tier`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─────────────────────────────────────────────────────────────
-- A.2  Партнёрский контур компенсации
-- ─────────────────────────────────────────────────────────────

-- Финансовый профиль партнёра (базовая ставка, рейтинг, override)
CREATE TABLE IF NOT EXISTS `partner_financial_profile` (
    `specialist_id`          BIGINT PRIMARY KEY,
    `base_revenue_pct`       DECIMAL(5,2) NOT NULL DEFAULT 50.00 COMMENT 'Базовая ставка % от выручки',
    `override_revenue_pct`   DECIMAL(5,2) DEFAULT NULL COMMENT 'Ручная ставка от директора (перекрывает base)',
    `rating`                 INT NOT NULL DEFAULT 100 COMMENT 'Рейтинг партнёра (начальный 100)',
    `rating_tier`            VARCHAR(32) NOT NULL DEFAULT 'standard',
    `is_active`              TINYINT(1) NOT NULL DEFAULT 1,
    `created_at`             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY `ix_pfp_rating` (`rating`),
    KEY `ix_pfp_active` (`is_active`),
    CONSTRAINT `fk_pfp_specialist`
        FOREIGN KEY (`specialist_id`) REFERENCES `specialists` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- История изменений рейтинга партнёра
CREATE TABLE IF NOT EXISTS `partner_rating_events` (
    `id`              BIGINT AUTO_INCREMENT PRIMARY KEY,
    `specialist_id`   BIGINT NOT NULL,
    `delta`           INT NOT NULL COMMENT 'Изменение рейтинга (+/-)',
    `reason`          VARCHAR(64) NOT NULL COMMENT 'complaint, bonus, monthly_review, manual',
    `comment`         VARCHAR(500) DEFAULT NULL,
    `initiated_by`    BIGINT DEFAULT NULL COMMENT 'specialists.id директора/админа (NULL = система)',
    `rating_before`   INT NOT NULL,
    `rating_after`    INT NOT NULL,
    `ref_entity_type` VARCHAR(32) DEFAULT NULL COMMENT 'chat, order и т.д.',
    `ref_entity_id`   BIGINT DEFAULT NULL,
    `created_at`      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY `ix_pre_specialist` (`specialist_id`, `created_at`),
    KEY `ix_pre_reason` (`reason`),
    CONSTRAINT `fk_pre_specialist`
        FOREIGN KEY (`specialist_id`) REFERENCES `specialists` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Месячные расчёты зарплаты партнёра
CREATE TABLE IF NOT EXISTS `partner_salary_periods` (
    `id`                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    `specialist_id`       BIGINT NOT NULL,
    `period_month`        CHAR(7) NOT NULL COMMENT 'Формат YYYY-MM',
    `status`              ENUM('draft','approved','paid') NOT NULL DEFAULT 'draft',
    `gross_revenue_rub`   DECIMAL(12,2) NOT NULL DEFAULT 0 COMMENT 'Общая выручка по партнёру за месяц',
    `effective_pct`       DECIMAL(5,2) NOT NULL DEFAULT 0 COMMENT 'Итоговый % (base/override + tier-бонус)',
    `base_salary_rub`     DECIMAL(12,2) NOT NULL DEFAULT 0 COMMENT 'gross_revenue * effective_pct / 100',
    `adjustments_rub`     DECIMAL(12,2) NOT NULL DEFAULT 0 COMMENT 'Сумма ручных корректировок',
    `final_salary_rub`    DECIMAL(12,2) NOT NULL DEFAULT 0 COMMENT 'base_salary + adjustments',
    `approved_by`         BIGINT DEFAULT NULL,
    `approved_at`         DATETIME DEFAULT NULL,
    `paid_at`             DATETIME DEFAULT NULL,
    `created_at`          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uq_psp_specialist_month` (`specialist_id`, `period_month`),
    KEY `ix_psp_status` (`status`),
    KEY `ix_psp_period` (`period_month`),
    CONSTRAINT `fk_psp_specialist`
        FOREIGN KEY (`specialist_id`) REFERENCES `specialists` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Ручные корректировки зарплаты от директора
CREATE TABLE IF NOT EXISTS `partner_salary_adjustments` (
    `id`                BIGINT AUTO_INCREMENT PRIMARY KEY,
    `salary_period_id`  BIGINT NOT NULL,
    `type`              ENUM('bonus','penalty','correction') NOT NULL,
    `amount_rub`        DECIMAL(12,2) NOT NULL COMMENT 'Положительная для бонуса, отрицательная для штрафа',
    `comment`           VARCHAR(500) NOT NULL,
    `created_by`        BIGINT NOT NULL COMMENT 'specialists.id директора',
    `created_at`        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY `ix_psa_period` (`salary_period_id`),
    CONSTRAINT `fk_psa_period`
        FOREIGN KEY (`salary_period_id`) REFERENCES `partner_salary_periods` (`id`) ON DELETE RESTRICT,
    CONSTRAINT `fk_psa_created_by`
        FOREIGN KEY (`created_by`) REFERENCES `specialists` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─────────────────────────────────────────────────────────────
-- A.3  Нормативные таблицы (справочники правил)
-- ─────────────────────────────────────────────────────────────

-- Правила начисления баллов по продуктам/событиям
CREATE TABLE IF NOT EXISTS `bonus_product_policies` (
    `id`                  INT AUTO_INCREMENT PRIMARY KEY,
    `product_type`        VARCHAR(64) NOT NULL COMMENT 'order, service, ofd, edo, kkt и т.д.',
    `event_type`          VARCHAR(64) NOT NULL COMMENT 'purchase, first_purchase, repeat_purchase',
    `points_fixed`        INT DEFAULT NULL COMMENT 'Фиксированное кол-во баллов (NULL = не используется)',
    `points_per_rub`      DECIMAL(8,4) DEFAULT NULL COMMENT 'Баллов за 1 рубль (NULL = не используется)',
    `points_lifetime_days` INT DEFAULT NULL COMMENT 'Срок жизни баллов (NULL = system default)',
    `min_order_rub`       INT DEFAULT NULL COMMENT 'Мин. сумма заказа для начисления',
    `max_points`          INT DEFAULT NULL COMMENT 'Потолок начисления за одну операцию',
    `is_active`           TINYINT(1) NOT NULL DEFAULT 1,
    `priority`            INT NOT NULL DEFAULT 0 COMMENT 'Приоритет: большее значение = выше',
    `valid_from`          DATETIME DEFAULT NULL,
    `valid_until`         DATETIME DEFAULT NULL,
    `created_at`          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY `ix_bpp_lookup` (`product_type`, `event_type`, `is_active`),
    KEY `ix_bpp_valid` (`valid_from`, `valid_until`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Пороговые уровни рейтинга (для клиентов и партнёров)
CREATE TABLE IF NOT EXISTS `rating_tiers` (
    `id`              INT AUTO_INCREMENT PRIMARY KEY,
    `target_type`     ENUM('client','partner') NOT NULL,
    `tier_name`       VARCHAR(32) NOT NULL COMMENT 'bronze, silver, gold, platinum',
    `min_rating`      INT NOT NULL COMMENT 'Минимальный рейтинг для попадания в уровень',
    `bonus_pct`       DECIMAL(5,2) NOT NULL DEFAULT 0 COMMENT 'Доп. % к начислению (для клиента) или к ставке (для партнёра)',
    `description`     VARCHAR(255) DEFAULT NULL,
    `is_active`       TINYINT(1) NOT NULL DEFAULT 1,
    `created_at`      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uq_rt_type_tier` (`target_type`, `tier_name`),
    KEY `ix_rt_lookup` (`target_type`, `min_rating`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Правила реферальных наград
CREATE TABLE IF NOT EXISTS `referral_campaign_rules` (
    `id`              INT AUTO_INCREMENT PRIMARY KEY,
    `reward_event`    VARCHAR(64) NOT NULL COMMENT 'activation, verification, qr_threshold',
    `actor_type`      ENUM('referrer','invited','both') NOT NULL DEFAULT 'referrer',
    `points_amount`   INT NOT NULL DEFAULT 0,
    `is_active`       TINYINT(1) NOT NULL DEFAULT 1,
    `valid_from`      DATETIME DEFAULT NULL,
    `valid_until`     DATETIME DEFAULT NULL,
    `description`     VARCHAR(255) DEFAULT NULL,
    `created_at`      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at`      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY `ix_rcr_lookup` (`reward_event`, `actor_type`, `is_active`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Начальные данные: уровни рейтинга клиентов
INSERT INTO `rating_tiers` (`target_type`, `tier_name`, `min_rating`, `bonus_pct`, `description`)
VALUES
    ('client', 'bronze',   0,   0.00, 'Начальный уровень'),
    ('client', 'silver',   100, 5.00, 'Серебряный — +5% к начислению баллов'),
    ('client', 'gold',     300, 10.00, 'Золотой — +10% к начислению баллов'),
    ('client', 'platinum', 500, 15.00, 'Платиновый — +15% к начислению баллов')
ON DUPLICATE KEY UPDATE `description` = VALUES(`description`);

-- Начальные данные: уровни рейтинга партнёров
INSERT INTO `rating_tiers` (`target_type`, `tier_name`, `min_rating`, `bonus_pct`, `description`)
VALUES
    ('partner', 'standard',  0,   0.00, 'Стандартный партнёр'),
    ('partner', 'advanced',  150, 3.00, 'Продвинутый — +3% к ставке'),
    ('partner', 'expert',    300, 5.00, 'Эксперт — +5% к ставке'),
    ('partner', 'top',       500, 8.00, 'Топ-партнёр — +8% к ставке')
ON DUPLICATE KEY UPDATE `description` = VALUES(`description`);

-- ─────────────────────────────────────────────────────────────
-- A.4  Расширение существующих таблиц
-- ─────────────────────────────────────────────────────────────

-- Используем процедуру для безопасного добавления колонок (MySQL 5.7/8.0 совместимость)
DROP PROCEDURE IF EXISTS _add_col_if_not_exists;
DELIMITER $$
CREATE PROCEDURE _add_col_if_not_exists(
    IN _tbl VARCHAR(64),
    IN _col VARCHAR(64),
    IN _def TEXT
)
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = _tbl
          AND COLUMN_NAME = _col
    ) THEN
        SET @ddl = CONCAT('ALTER TABLE `', _tbl, '` ADD COLUMN `', _col, '` ', _def);
        PREPARE stmt FROM @ddl;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END$$
DELIMITER ;

-- system_settings: добавить group_name, sort_order, unit
CALL _add_col_if_not_exists('system_settings', 'group_name',
    "VARCHAR(64) DEFAULT NULL COMMENT 'Группа настройки для UI'");
CALL _add_col_if_not_exists('system_settings', 'sort_order',
    "INT NOT NULL DEFAULT 0 COMMENT 'Порядок отображения в группе'");
CALL _add_col_if_not_exists('system_settings', 'unit',
    "VARCHAR(32) DEFAULT NULL COMMENT 'Единица измерения: %, баллы, руб, дни'");

-- Расширить value_type для поддержки json
ALTER TABLE `system_settings`
    MODIFY COLUMN `value_type` ENUM('number', 'text', 'json') NOT NULL;

-- support_chat_payments: добавить поля бонусной экономики
CALL _add_col_if_not_exists('support_chat_payments', 'gross_amount_rub',
    "INT DEFAULT NULL COMMENT 'Полная сумма до скидки'");
CALL _add_col_if_not_exists('support_chat_payments', 'bonus_discount_rub',
    "INT NOT NULL DEFAULT 0 COMMENT 'Скидка за счёт баллов (в рублях)'");
CALL _add_col_if_not_exists('support_chat_payments', 'net_amount_rub',
    "INT DEFAULT NULL COMMENT 'Итого к оплате (gross - bonus_discount)'");
CALL _add_col_if_not_exists('support_chat_payments', 'bonus_points_spent',
    "INT NOT NULL DEFAULT 0 COMMENT 'Списано баллов'");
CALL _add_col_if_not_exists('support_chat_payments', 'bonus_rate_snapshot',
    "DECIMAL(8,4) DEFAULT NULL COMMENT 'Курс баллы→руб на момент оплаты'");
CALL _add_col_if_not_exists('support_chat_payments', 'salary_base_amount_rub',
    "INT DEFAULT NULL COMMENT 'База для расчёта зарплаты партнёра (= gross, не net)'");

-- referral_activations: добавить поля для QR-трекинга
CALL _add_col_if_not_exists('referral_activations', 'scan_source',
    "VARCHAR(32) DEFAULT NULL COMMENT 'qr, deep_link, manual'");
CALL _add_col_if_not_exists('referral_activations', 'verification_status',
    "ENUM('pending','verified','rejected') DEFAULT NULL");
CALL _add_col_if_not_exists('referral_activations', 'verification_at',
    "DATETIME DEFAULT NULL");
CALL _add_col_if_not_exists('referral_activations', 'unique_hash',
    "VARCHAR(64) DEFAULT NULL COMMENT 'Хэш для дедупликации QR-сканов'");

DROP PROCEDURE IF EXISTS _add_col_if_not_exists;

-- Индекс для поиска по unique_hash (если заполнен)
SET @idx_exists = (SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'referral_activations'
    AND INDEX_NAME = 'ix_ra_unique_hash');
SET @sql = IF(@idx_exists = 0,
    'CREATE INDEX `ix_ra_unique_hash` ON `referral_activations` (`unique_hash`)',
    'SELECT 1');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- ─────────────────────────────────────────────────────────────
-- A.5  Начальные ключи system_settings
-- ─────────────────────────────────────────────────────────────

INSERT INTO `system_settings` (`name`, `value_type`, `number_value`, `char_value`, `description`, `group_name`, `sort_order`, `unit`)
VALUES
    -- Feature flags
    ('LOYALTY_ENABLED', 'text', NULL, '0',
     'Включить систему лояльности (0/1)', 'loyalty', 1, NULL),

    -- Курсы конвертации
    ('LOYALTY_RUB_TO_POINTS_RATE', 'number', 10.0000, NULL,
     'Сколько баллов за 1 рубль покупки', 'loyalty', 10, 'баллы/руб'),
    ('LOYALTY_POINTS_TO_RUB_RATE', 'number', 0.1000, NULL,
     'Сколько рублей стоит 1 балл при списании', 'loyalty', 11, 'руб/балл'),

    -- Лимиты
    ('LOYALTY_MAX_REDEEM_SHARE_PCT', 'number', 30.0000, NULL,
     'Макс. % от суммы заказа, оплачиваемый баллами', 'loyalty', 20, '%'),

    -- Бонусы за события
    ('LOYALTY_FIRST_PURCHASE_BONUS', 'number', 50.0000, NULL,
     'Бонус за первую покупку (баллы)', 'loyalty', 30, 'баллы'),
    ('LOYALTY_PROFILE_FIELD_POINTS', 'number', 10.0000, NULL,
     'Бонус за заполнение одного поля профиля', 'loyalty', 31, 'баллы'),

    -- Срок жизни
    ('LOYALTY_DEFAULT_POINTS_LIFETIME_DAYS', 'number', 365.0000, NULL,
     'Срок жизни баллов по умолчанию (дни)', 'loyalty', 40, 'дни'),

    -- Реферальная программа
    ('LOYALTY_REFERRAL_VERIFICATION_POINTS', 'number', 100.0000, NULL,
     'Бонус рефереру за верификацию приглашённого', 'loyalty_referral', 50, 'баллы'),
    ('LOYALTY_QR_COLLECTION_THRESHOLD', 'number', 10.0000, NULL,
     'Порог QR-сканов для получения награды', 'loyalty_referral', 51, 'шт'),
    ('LOYALTY_QR_COLLECTION_REWARD_POINTS', 'number', 200.0000, NULL,
     'Награда за достижение QR-порога', 'loyalty_referral', 52, 'баллы'),

    -- Партнёрская компенсация
    ('LOYALTY_PARTNER_DEFAULT_BASE_REVENUE_PCT', 'number', 50.0000, NULL,
     'Базовая ставка партнёра (% от выручки)', 'partner', 60, '%'),
    ('LOYALTY_PARTNER_INITIAL_RATING', 'number', 100.0000, NULL,
     'Начальный рейтинг нового партнёра', 'partner', 61, NULL),
    ('LOYALTY_PARTNER_COMPLAINT_RATING_PENALTY', 'number', -10.0000, NULL,
     'Штраф рейтинга за жалобу клиента', 'partner', 62, NULL)

ON DUPLICATE KEY UPDATE `description` = VALUES(`description`);

SELECT 'A.5: system_settings — добавлены' AS result;

SELECT 'Миграция 011: Фундамент финансовой модели — применена!' AS result;
