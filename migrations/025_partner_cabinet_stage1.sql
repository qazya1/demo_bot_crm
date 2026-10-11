-- Stage 1: partner cabinet (profession=5)
ALTER TABLE users ADD COLUMN added_by_partner_id BIGINT NULL;
ALTER TABLE users ADD COLUMN source_partner_id BIGINT NULL;
ALTER TABLE users ADD COLUMN partner_source_type VARCHAR(32) NULL;

CREATE INDEX ix_users_added_by_partner ON users(added_by_partner_id);
CREATE INDEX ix_users_source_partner ON users(source_partner_id);
CREATE INDEX ix_users_partner_source_type ON users(partner_source_type);
