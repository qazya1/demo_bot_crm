-- 020_referral_qr_collection.sql
-- Явная миграция для хранения прогресса QR collection (обмен QR между реферером и приглашённым).

CREATE TABLE IF NOT EXISTS referral_qr_collection (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  owner_user_id INT NOT NULL,
  invited_user_id INT NOT NULL,
  collected_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  UNIQUE KEY uq_owner_invited (owner_user_id, invited_user_id),
  KEY ix_owner (owner_user_id),
  KEY ix_invited (invited_user_id),

  CONSTRAINT fk_rqc_owner_user
    FOREIGN KEY (owner_user_id) REFERENCES users(id)
    ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_rqc_invited_user
    FOREIGN KEY (invited_user_id) REFERENCES users(id)
    ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
