-- ==============================================================================
-- МИГРАЦИЯ 021: Настройка реферальной надбавки партнёра
-- Дата: 2026-03-31
-- Описание: Добавляет global-setting LOYALTY_PARTNER_REFERRAL_UPLIFT_PCT
-- ==============================================================================

USE bot_db;

INSERT INTO system_settings (name, value_type, number_value, char_value, description)
VALUES (
    'LOYALTY_PARTNER_REFERRAL_UPLIFT_PCT',
    'number',
    0,
    NULL,
    'Реферальная надбавка партнёру, % от выручки приглашённых им клиентов'
)
ON DUPLICATE KEY UPDATE
    value_type = VALUES(value_type),
    description = VALUES(description);

SELECT 'Миграция 021: LOYALTY_PARTNER_REFERRAL_UPLIFT_PCT — применена!' AS result;
