CREATE TABLE IF NOT EXISTS memory_recalls (
 user_id TEXT NOT NULL, scope_id TEXT NOT NULL, query TEXT NOT NULL,
 result TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(user_id,scope_id)
);
CREATE TABLE IF NOT EXISTS group_memory_life (
 memory_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
 permanent INTEGER NOT NULL DEFAULT 0, reinforced_at INTEGER,
 recall_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mood_states (
 user_id TEXT NOT NULL, scope_id TEXT NOT NULL, value REAL NOT NULL,
 emotions TEXT NOT NULL DEFAULT '{}', updated_at INTEGER NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1,
 generation INTEGER NOT NULL DEFAULT 0, last_judged_at INTEGER NOT NULL DEFAULT 0,
 last_source_at INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(user_id,scope_id)
);
CREATE TABLE IF NOT EXISTS mood_events (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, scope_id TEXT NOT NULL,
 source_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
 confidence REAL NOT NULL DEFAULT 0, delta REAL NOT NULL DEFAULT 0,
 value REAL NOT NULL DEFAULT 0, emotions TEXT NOT NULL DEFAULT '{}',
 significant INTEGER NOT NULL DEFAULT 0, injected_at INTEGER,
    consumed_at INTEGER,
 created_at INTEGER NOT NULL, UNIQUE(user_id,scope_id,source_id)
);
CREATE INDEX IF NOT EXISTS mood_timeline ON mood_events(user_id,scope_id,created_at);
