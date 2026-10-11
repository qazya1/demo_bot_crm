-- ==============================================================================
-- МИГРАЦИЯ 022: support_chat_payments — колонки бонусной оплаты
-- Дата: 2026-04-01
-- Описание:
--   Добавляет отсутствующие колонки для чат-платежей:
--   bonus_points_applied, bonus_discount_rub, cash_amount_rub,
--   bonus_reservation_key, payment_method, paid_at
-- ==============================================================================

USE bot_db;

-- bonus_points_applied
SET @col_exists = (
    SELECT COUNT(*)
      FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 'support_chat_payments'
       AND column_name = 'bonus_points_applied'
);
SET @sql = IF(
    @col_exists = 0,
    'ALTER TABLE support_chat_payments ADD COLUMN bonus_points_applied INT NOT NULL DEFAULT 0 AFTER status',
    'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- bonus_discount_rub
SET @col_exists = (
    SELECT COUNT(*)
      FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 'support_chat_payments'
       AND column_name = 'bonus_discount_rub'
);
SET @sql = IF(
    @col_exists = 0,
    'ALTER TABLE support_chat_payments ADD COLUMN bonus_discount_rub INT NOT NULL DEFAULT 0 AFTER bonus_points_applied',
    'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- cash_amount_rub
SET @col_exists = (
    SELECT COUNT(*)
      FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 'support_chat_payments'
       AND column_name = 'cash_amount_rub'
);
SET @sql = IF(
    @col_exists = 0,
    'ALTER TABLE support_chat_payments ADD COLUMN cash_amount_rub INT NOT NULL DEFAULT 0 AFTER bonus_discount_rub',
    'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- bonus_reservation_key
SET @col_exists = (
    SELECT COUNT(*)
      FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 'support_chat_payments'
       AND column_name = 'bonus_reservation_key'
);
SET @sql = IF(
    @col_exists = 0,
    'ALTER TABLE support_chat_payments ADD COLUMN bonus_reservation_key VARCHAR(128) NULL AFTER cash_amount_rub',
    'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- payment_method
SET @col_exists = (
    SELECT COUNT(*)
      FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 'support_chat_payments'
       AND column_name = 'payment_method'
);
SET @sql = IF(
    @col_exists = 0,
    'ALTER TABLE support_chat_payments ADD COLUMN payment_method VARCHAR(32) NOT NULL DEFAULT ''CASH_ONLY'' AFTER bonus_reservation_key',
    'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- paid_at
SET @col_exists = (
    SELECT COUNT(*)
      FROM information_schema.columns
     WHERE table_schema = DATABASE()
       AND table_name = 'support_chat_payments'
       AND column_name = 'paid_at'
);
SET @sql = IF(
    @col_exists = 0,
    'ALTER TABLE support_chat_payments ADD COLUMN paid_at TIMESTAMP NULL DEFAULT NULL AFTER payment_method',
    'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Индексы
SET @idx_exists = (
    SELECT COUNT(*)
      FROM information_schema.statistics
     WHERE table_schema = DATABASE()
       AND table_name = 'support_chat_payments'
       AND index_name = 'ix_pay_chat_status'
);
SET @sql = IF(
    @idx_exists = 0,
    'ALTER TABLE support_chat_payments ADD INDEX ix_pay_chat_status (chat_id, status)',
    'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists = (
    SELECT COUNT(*)
      FROM information_schema.statistics
     WHERE table_schema = DATABASE()
       AND table_name = 'support_chat_payments'
       AND index_name = 'ix_pay_paid_at'
);
SET @sql = IF(
    @idx_exists = 0,
    'ALTER TABLE support_chat_payments ADD INDEX ix_pay_paid_at (paid_at)',
    'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SELECT 'Миграция 022: support_chat_payments bonus columns — применена!' AS result;
