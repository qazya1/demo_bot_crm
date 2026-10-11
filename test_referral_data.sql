-- ============================================================
-- Тестовые данные для проверки аналитики реферальных ссылок
-- Добавляет колонку source_platform (если нет), 4 тестовых
-- пользователя и привязывает их к ПЕРВОМУ реферальному коду.
-- ============================================================

-- 1) Миграция: добавить source_platform если ещё нет
ALTER TABLE referral_activations
  ADD COLUMN source_platform VARCHAR(16) NULL;
-- Если колонка уже есть, запрос упадёт — просто проигнорируй ошибку и иди дальше.

-- 2) Тестовые пользователи (2 из TG, 2 из Max)
INSERT INTO users (tg_user_id, max_user_id, first_name, last_name, phone, city, is_verified, tg_username)
VALUES
  (900000001, NULL,       'Алексей',  'Тестов',    '+79001111111', 'Москва',     1, 'alexey_test'),
  (900000002, NULL,       'Мария',    'Примерова', '+79002222222', 'Волгоград',  0, 'maria_test'),
  (NULL,      800000001,  'Дмитрий',  'Максов',    '+79003333333', 'Казань',     1, NULL),
  (NULL,      800000002,  'Елена',    'Ботова',    '+79004444444', 'Волгоград',  1, NULL);

-- 3) Привязка к первому реферальному коду
-- Подставь ID первого кода (посмотри SELECT id FROM referral_codes LIMIT 1)
SET @rc_id = (SELECT id FROM referral_codes ORDER BY id LIMIT 1);

-- Находим ID только что вставленных пользователей
SET @u1 = (SELECT id FROM users WHERE phone = '+79001111111' LIMIT 1);
SET @u2 = (SELECT id FROM users WHERE phone = '+79002222222' LIMIT 1);
SET @u3 = (SELECT id FROM users WHERE phone = '+79003333333' LIMIT 1);
SET @u4 = (SELECT id FROM users WHERE phone = '+79004444444' LIMIT 1);

INSERT INTO referral_activations (user_id, referral_code_id, raw_text, source_platform)
VALUES
  (@u1, @rc_id, 'ref_test_tg_1',  'telegram'),
  (@u2, @rc_id, 'ref_test_tg_2',  'telegram'),
  (@u3, @rc_id, 'ref_test_max_1', 'max'),
  (@u4, @rc_id, 'ref_test_max_2', 'max');

-- Готово! Теперь на странице /analytics/ у первого кода должны появиться
-- 4 пользователя: 2 с бейджем TG и 2 с бейджем Max.
-- Чтобы удалить тестовые данные:
-- DELETE FROM referral_activations WHERE raw_text LIKE 'ref_test_%';
-- DELETE FROM users WHERE phone IN ('+79001111111','+79002222222','+79003333333','+79004444444');
