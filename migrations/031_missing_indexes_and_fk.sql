-- ================================================================
-- Migration 031: Add missing indexes and foreign keys
-- ================================================================

-- ----------------------------------------------------------------
-- 1. orders: индексы на часто используемые поля фильтрации
-- ----------------------------------------------------------------
ALTER TABLE orders
  ADD KEY idx_orders_status       (status),
  ADD KEY idx_orders_created_at   (created_at),
  ADD KEY idx_orders_specialist_id (specialist_id),
  ADD KEY idx_orders_company_id   (company_id);

-- ----------------------------------------------------------------
-- 2. client_kkt: индекс + FK на company_id → companies.id
--    Сначала обнуляем висячие ссылки на несуществующие компании
-- ----------------------------------------------------------------
UPDATE client_kkt
SET company_id = NULL
WHERE company_id IS NOT NULL
  AND company_id NOT IN (SELECT id FROM companies);

ALTER TABLE client_kkt
  ADD KEY idx_client_kkt_company_id (company_id),
  ADD CONSTRAINT fk_client_kkt_company
    FOREIGN KEY (company_id) REFERENCES companies(id)
    ON DELETE SET NULL ON UPDATE CASCADE;

-- ----------------------------------------------------------------
-- 3. company_contacts: FK на contact_id → users.id
--    Удаляем строки с contact_id, не существующим в users
-- ----------------------------------------------------------------
DELETE FROM company_contacts
WHERE contact_id NOT IN (SELECT id FROM users);

ALTER TABLE company_contacts
  ADD CONSTRAINT fk_company_contacts_contact
    FOREIGN KEY (contact_id) REFERENCES users(id)
    ON DELETE CASCADE ON UPDATE CASCADE;

-- ----------------------------------------------------------------
-- 4. tasks: FK на assignee_id, director_id, created_by → specialists.id
--           FK на client_id → users.id
--
--    created_by объявлен NOT NULL в схеме, но FK без ON DELETE ACTION
--    может нарушиться при удалении специалиста. Меняем на NULL-able
--    (если специалист удалён — аудиторная цепочка рвётся в любом случае).
-- ----------------------------------------------------------------

-- 4a. Обнуляем nullable-поля с висячими ссылками
UPDATE tasks SET assignee_id = NULL
WHERE assignee_id IS NOT NULL
  AND assignee_id NOT IN (SELECT id FROM specialists);

UPDATE tasks SET director_id = NULL
WHERE director_id IS NOT NULL
  AND director_id NOT IN (SELECT id FROM specialists);

UPDATE tasks SET client_id = NULL
WHERE client_id IS NOT NULL
  AND client_id NOT IN (SELECT id FROM users);

-- 4b. created_by NOT NULL — удаляем задачи с несуществующим автором
DELETE FROM task_clients WHERE task_id IN (
  SELECT id FROM tasks
  WHERE created_by NOT IN (SELECT id FROM specialists)
);
DELETE FROM task_history WHERE task_id IN (
  SELECT id FROM tasks
  WHERE created_by NOT IN (SELECT id FROM specialists)
);
DELETE FROM tasks
WHERE created_by NOT IN (SELECT id FROM specialists);

-- 4c. Добавляем FK
ALTER TABLE tasks MODIFY COLUMN client_id INT DEFAULT NULL;
ALTER TABLE specialists MODIFY COLUMN linked_user_id INT DEFAULT NULL;

ALTER TABLE tasks
  ADD CONSTRAINT fk_tasks_assignee
    FOREIGN KEY (assignee_id) REFERENCES specialists(id)
    ON DELETE SET NULL ON UPDATE CASCADE,
  ADD CONSTRAINT fk_tasks_director
    FOREIGN KEY (director_id) REFERENCES specialists(id)
    ON DELETE SET NULL ON UPDATE CASCADE,
  ADD CONSTRAINT fk_tasks_created_by
    FOREIGN KEY (created_by) REFERENCES specialists(id)
    ON DELETE RESTRICT ON UPDATE CASCADE,
  ADD CONSTRAINT fk_tasks_client
    FOREIGN KEY (client_id) REFERENCES users(id)
    ON DELETE SET NULL ON UPDATE CASCADE;

-- ----------------------------------------------------------------
-- 5. specialists.linked_user_id → users.id
--    Обнуляем висячие ссылки
-- ----------------------------------------------------------------
UPDATE specialists SET linked_user_id = NULL
WHERE linked_user_id IS NOT NULL
  AND linked_user_id NOT IN (SELECT id FROM users);

ALTER TABLE specialists
  ADD CONSTRAINT fk_specialists_linked_user
    FOREIGN KEY (linked_user_id) REFERENCES users(id)
    ON DELETE SET NULL ON UPDATE CASCADE;
