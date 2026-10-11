-- Добавляем поле адреса в таблицу пользователей и заказов

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS address TEXT DEFAULT NULL;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS address TEXT DEFAULT NULL;
