-- Пункт 1 PLAN_SAITA: добавить source в orders
ALTER TABLE orders ADD COLUMN source VARCHAR(32) DEFAULT NULL AFTER status;
