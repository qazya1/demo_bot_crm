-- Canonicalize support_chats.client_user_id -> users.id
-- Run manually: mysql -u root -p bot_db < migrations/006_support_chats_internal_client_id.sql

START TRANSACTION;

-- 1) Временная колонка под новый канон.
ALTER TABLE support_chats
  ADD COLUMN client_user_id_new BIGINT NULL AFTER client_user_id;

-- 2) Переносим значения в users.id:
--    приоритет:
--      a) уже internal users.id
--      b) tg_user_id
--      c) max_user_id
UPDATE support_chats sc
LEFT JOIN users u_by_id
  ON u_by_id.id = CAST(sc.client_user_id AS UNSIGNED)
LEFT JOIN users u_by_tg
  ON u_by_tg.tg_user_id = CAST(sc.client_user_id AS UNSIGNED)
LEFT JOIN users u_by_max
  ON u_by_max.max_user_id = CAST(sc.client_user_id AS UNSIGNED)
SET sc.client_user_id_new = COALESCE(u_by_id.id, u_by_tg.id, u_by_max.id);

-- 3) Остановиться, если остались неразрешённые чаты.
--    Этот SELECT должен вернуть 0 строк перед продолжением.
SELECT id, client_user_id
FROM support_chats
WHERE client_user_id_new IS NULL;

-- 4) Только после ручной проверки, что SELECT выше пуст, выполнять блок ниже:
-- ALTER TABLE support_chats DROP INDEX ix_client_prof;
-- ALTER TABLE support_chats DROP COLUMN client_user_id;
-- ALTER TABLE support_chats CHANGE COLUMN client_user_id_new client_user_id BIGINT NOT NULL;
-- ALTER TABLE support_chats ADD KEY ix_client_prof (client_user_id, profession);

COMMIT;
