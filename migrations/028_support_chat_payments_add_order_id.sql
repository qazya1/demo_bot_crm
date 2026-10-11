-- Добавляем явную ссылку заказ ↔ платёж чата
-- support_chat_payments.order_id → orders.id

ALTER TABLE support_chat_payments
    ADD COLUMN IF NOT EXISTS order_id INT DEFAULT NULL;

-- Заполняем уже существующие данные через совпадение payment_id
UPDATE support_chat_payments scp
    JOIN orders o ON o.payment_id = scp.payment_id
                  AND o.payment_id IS NOT NULL
                  AND o.payment_id != ''
SET scp.order_id = o.id
WHERE scp.order_id IS NULL;

ALTER TABLE support_chat_payments
    ADD INDEX IF NOT EXISTS idx_scp_order_id (order_id);
