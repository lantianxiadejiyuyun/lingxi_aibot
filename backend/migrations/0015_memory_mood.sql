ALTER TABLE memories ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'fact';
ALTER TABLE memories ADD COLUMN semantic_key TEXT NOT NULL DEFAULT '';
ALTER TABLE memories ADD COLUMN reinforced_at INTEGER;
ALTER TABLE memories ADD COLUMN recall_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memories ADD COLUMN superseded_by TEXT;
CREATE INDEX memory_semantic ON memories(user_id,semantic_key,deleted_at,superseded_by);
CREATE TABLE memory_recalls (
 user_id TEXT NOT NULL, scope_id TEXT NOT NULL, query TEXT NOT NULL,
 result TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(user_id,scope_id)
);
CREATE TABLE memory_auto_limits (
 user_id TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(user_id,day)
);
CREATE TABLE mood_states (
 user_id TEXT NOT NULL, scope_id TEXT NOT NULL, value REAL NOT NULL,
 emotions TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
 generation INTEGER NOT NULL DEFAULT 0, last_judged_at INTEGER NOT NULL DEFAULT 0,
 last_source_at INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(user_id,scope_id)
);
CREATE TABLE mood_events (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, scope_id TEXT NOT NULL,
 source_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
 confidence REAL NOT NULL DEFAULT 0, delta REAL NOT NULL DEFAULT 0,
 value REAL NOT NULL DEFAULT 0, emotions TEXT NOT NULL DEFAULT '{}',
 significant INTEGER NOT NULL DEFAULT 0, injected_at INTEGER,
    consumed_at INTEGER,
 created_at INTEGER NOT NULL, UNIQUE(user_id,scope_id,source_id)
);
CREATE INDEX mood_timeline ON mood_events(user_id,scope_id,created_at);
