-- Миграция 024: Добавление поля is_read для отслеживания прочитанных сообщений
-- Добавляет поле is_read в таблицу support_chat_messages для индикации непрочитанных

ALTER TABLE support_chat_messages
ADD COLUMN IF NOT EXISTS is_read TINYINT NOT NULL DEFAULT 1 AFTER text;

-- Все существующие сообщения помечаем как прочитанные
UPDATE support_chat_messages SET is_read = 1 WHERE is_read IS NULL;

-- Индекс для быстрого поиска непрочитанных сообщений от клиента
CREATE INDEX IF NOT EXISTS ix_msg_unread ON support_chat_messages(chat_id, sender, is_read);
