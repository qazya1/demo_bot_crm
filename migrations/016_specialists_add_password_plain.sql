-- Миграция: удалить поле password_plain (пароли хранятся только в хешированном виде)
-- Если колонка была добавлена ранее — удаляем. Если нет — пропускаем.

ALTER TABLE specialists DROP COLUMN IF EXISTS password_plain;
