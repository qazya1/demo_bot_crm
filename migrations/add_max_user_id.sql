-- Фаза 1: Добавление поддержки MAX Messenger в таблицу users
-- Запустить вручную: mysql -u root -p bot_db < migrations/add_max_user_id.sql

ALTER TABLE users
  ADD COLUMN max_user_id BIGINT NULL UNIQUE AFTER tg_user_id;
