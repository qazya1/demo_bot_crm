-- Миграция 015: Добавление поддержки редактирования сообщений
-- Добавляет поля edited_at, edited_by, is_edited в таблицу support_chat_messages

-- Добавляем поле edited_at (время редактирования)
ALTER TABLE support_chat_messages 
ADD COLUMN IF NOT EXISTS edited_at TIMESTAMP NULL DEFAULT NULL AFTER text;

-- Добавляем поле edited_by (кто отредактировал - internal users.id)
ALTER TABLE support_chat_messages 
ADD COLUMN IF NOT EXISTS edited_by BIGINT NULL DEFAULT NULL AFTER edited_at;

-- Добавляем поле is_edited (флаг редактирования)
ALTER TABLE support_chat_messages 
ADD COLUMN IF NOT EXISTS is_edited TINYINT NOT NULL DEFAULT 0 AFTER edited_by;

-- Добавляем индекс для быстрого поиска отредактированных сообщений
CREATE INDEX IF NOT EXISTS ix_msg_edited ON support_chat_messages(is_edited);
