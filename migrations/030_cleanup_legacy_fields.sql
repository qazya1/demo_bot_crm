-- ============================================================
-- Migration 030: Remove dead columns and tables
-- ============================================================

-- ----------------------------------------------------------------
-- 1. referral_codes: finish user_id → owner_user_id migration,
--    add FK on owner_user_id, drop legacy user_id column
-- ----------------------------------------------------------------
UPDATE referral_codes
SET owner_user_id = user_id
WHERE owner_user_id IS NULL AND user_id IS NOT NULL;

ALTER TABLE referral_codes
  DROP FOREIGN KEY fk_ref_codes_users,
  DROP COLUMN user_id;

ALTER TABLE referral_codes
  ADD CONSTRAINT fk_ref_codes_owner_user
    FOREIGN KEY (owner_user_id) REFERENCES users(id)
    ON DELETE SET NULL ON UPDATE CASCADE;

-- ----------------------------------------------------------------
-- 2. abonents: drop dead columns (entity_type, client_user_id, phone)
--    and the duplicate index on phone_e164
-- ----------------------------------------------------------------
ALTER TABLE abonents
  DROP COLUMN entity_type,
  DROP COLUMN client_user_id,
  DROP COLUMN phone,
  DROP INDEX ix_abonents_phone_e164;

-- ----------------------------------------------------------------
-- 3. support_chat_tokens: drop duplicate index on token
--    (UNIQUE KEY `token` already covers it)
-- ----------------------------------------------------------------
ALTER TABLE support_chat_tokens
  DROP INDEX idx_chat_tokens_token;

-- ----------------------------------------------------------------
-- 4. Drop legacy ФН/ОФД/маркировка tables
--    (all data lives in client_kkt columns; these tables are never
--    read or written by any application code)
-- ----------------------------------------------------------------
DROP TABLE IF EXISTS client_fn;
DROP TABLE IF EXISTS client_ofd;
DROP TABLE IF EXISTS client_mark;
