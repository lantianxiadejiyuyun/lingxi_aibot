CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, group_id TEXT NOT NULL, qq_id TEXT NOT NULL,
 name TEXT NOT NULL, content TEXT NOT NULL, triggered INTEGER NOT NULL,
 occurred_at INTEGER NOT NULL, created_at INTEGER NOT NULL, reply TEXT NOT NULL DEFAULT '',
 delivery_status TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS events_person ON events(user_id,group_id,qq_id,occurred_at);
-- One stable group conversation per account/group. Message bodies remain in their owning DB.
CREATE TABLE IF NOT EXISTS group_sessions (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, group_id TEXT NOT NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 lease_token TEXT, lease_until INTEGER,
 UNIQUE(user_id,group_id)
);
CREATE TABLE IF NOT EXISTS facts (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, group_id TEXT NOT NULL, qq_id TEXT NOT NULL,
 name TEXT NOT NULL, fact_key TEXT NOT NULL, content TEXT NOT NULL,
 source TEXT NOT NULL, confidence REAL NOT NULL, evidence TEXT NOT NULL,
 expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 deleted_at INTEGER, revision INTEGER NOT NULL DEFAULT 1, indexed_revision INTEGER NOT NULL DEFAULT 0,
 UNIQUE(user_id,group_id,qq_id,fact_key)
);
CREATE INDEX IF NOT EXISTS facts_person ON facts(user_id,group_id,qq_id,deleted_at);

-- Collection bytes are committed atomically with metadata, outside personal memory.
CREATE TABLE IF NOT EXISTS stickers (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, content_hash TEXT NOT NULL,
 title TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]', reason TEXT NOT NULL,
 collected_by TEXT NOT NULL, favorite INTEGER NOT NULL DEFAULT 0,
 mime TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, byte_size INTEGER NOT NULL,
 original BLOB, thumbnail BLOB, group_id TEXT NOT NULL, qq_id TEXT NOT NULL,
 occurrences INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 deleted_at INTEGER, UNIQUE(user_id,content_hash)
);
CREATE INDEX IF NOT EXISTS stickers_user ON stickers(user_id,deleted_at,updated_at);
CREATE TABLE IF NOT EXISTS sticker_inbox (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, group_id TEXT NOT NULL, qq_id TEXT NOT NULL,
 forced INTEGER NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 reason TEXT NOT NULL DEFAULT '', sticker_id TEXT, content_hash TEXT,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sticker_inbox_user ON sticker_inbox(user_id,status,created_at);
CREATE TABLE IF NOT EXISTS sticker_review_limits (
 user_id TEXT NOT NULL, hour INTEGER NOT NULL, count INTEGER NOT NULL,
 PRIMARY KEY(user_id,hour)
);
