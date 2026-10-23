-- The owner's private images stay in the primary database, never in qq/people.sqlite3.
-- Table names mirror the group store so every operation selects its explicit scope.
CREATE TABLE stickers (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 content_hash TEXT NOT NULL, title TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]', reason TEXT NOT NULL,
 collected_by TEXT NOT NULL, favorite INTEGER NOT NULL DEFAULT 0,
 mime TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL, byte_size INTEGER NOT NULL,
 original BLOB, thumbnail BLOB, group_id TEXT NOT NULL CHECK(group_id=''), qq_id TEXT NOT NULL,
 occurrences INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 deleted_at INTEGER, UNIQUE(user_id,content_hash)
);
CREATE INDEX stickers_user ON stickers(user_id,deleted_at,updated_at);
CREATE TABLE sticker_inbox (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 group_id TEXT NOT NULL CHECK(group_id=''), qq_id TEXT NOT NULL,
 forced INTEGER NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 reason TEXT NOT NULL DEFAULT '', sticker_id TEXT, content_hash TEXT,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX sticker_inbox_user ON sticker_inbox(user_id,status,created_at);
CREATE TABLE sticker_review_limits (
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 hour INTEGER NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(user_id,hour)
);
