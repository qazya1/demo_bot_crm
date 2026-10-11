-- Добавление полей для ручных сделок
-- orders: description, author_name
-- order_items: legacy_item_type, legacy_item_title

ALTER TABLE orders ADD COLUMN description TEXT DEFAULT NULL AFTER payment_info;
ALTER TABLE orders ADD COLUMN author_name VARCHAR(255) DEFAULT NULL AFTER description;

ALTER TABLE order_items ADD COLUMN legacy_item_type VARCHAR(255) DEFAULT NULL;
ALTER TABLE order_items ADD COLUMN legacy_item_title VARCHAR(255) DEFAULT NULL;
