-- Runtime-owned credentials are deliberately separate from user-issued bridge tokens.
ALTER TABLE feishu_state ADD COLUMN managed_token_hash TEXT;
CREATE UNIQUE INDEX feishu_managed_token ON feishu_state(managed_token_hash);
