-- M2M связь задач и клиентов для CRM
CREATE TABLE IF NOT EXISTS task_clients (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id BIGINT NOT NULL,
    client_id INT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_task_client (task_id, client_id),
    KEY idx_task_clients_client (client_id),
    CONSTRAINT fk_task_clients_task
        FOREIGN KEY (task_id) REFERENCES tasks(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_task_clients_client
        FOREIGN KEY (client_id) REFERENCES users(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
