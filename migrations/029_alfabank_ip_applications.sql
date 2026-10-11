-- ==============================================================================
-- МИГРАЦИЯ 029: Заявки на оформление ИП через API Альфа-Банка
-- Дата: 2026-04-30
-- Описание: Таблица для хранения заявок пользователей на открытие ИП
--           через партнёрский API Альфа-Банка.
--
-- Поля:
--   id            — внутренний PK
--   user_id       — FK на users.id (внутренний идентификатор)
--   tg_user_id    — telegram user id (для удобной выборки в боте)
--   alfabank_id   — id заявки в системе Альфа-Банка (возвращается в ответе /leads)
--   status        — текущий локальный статус заявки:
--                     pending  — ждём решения банка
--                     approved — банк принял
--                     declined — банк отклонил
--   alfabank_state— "сырое" значение state из Alfa-Bank API (для отладки)
--   alfabank_raw  — сырой JSON последнего ответа банка (TEXT)
--   created_at / updated_at
-- ==============================================================================

USE bot_db;

CREATE TABLE IF NOT EXISTS alfabank_ip_applications (
    id              INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    user_id         INT NOT NULL,
    tg_user_id      BIGINT DEFAULT NULL,
    alfabank_id     VARCHAR(128) DEFAULT NULL,
    status          ENUM('pending','approved','declined') NOT NULL DEFAULT 'pending',
    alfabank_state  VARCHAR(64) DEFAULT NULL,
    alfabank_raw    TEXT DEFAULT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_alfabank_ip_user (user_id),
    KEY idx_alfabank_ip_tg (tg_user_id),
    KEY idx_alfabank_ip_status (status),
    UNIQUE KEY uq_alfabank_ip_alfabank_id (alfabank_id),
    CONSTRAINT fk_alfabank_ip_user
        FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

SELECT 'Миграция 029: alfabank_ip_applications — применена!' AS result;
