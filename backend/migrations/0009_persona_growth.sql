CREATE TABLE persona_growth (
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 profile_id TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 0,
 watermark INTEGER NOT NULL DEFAULT 0,
 next_at INTEGER NOT NULL DEFAULT 0,
 lease TEXT,
 lease_until INTEGER NOT NULL DEFAULT 0,
 last_reflected_at INTEGER,
 last_error TEXT NOT NULL DEFAULT '',
 created_at INTEGER NOT NULL,
 PRIMARY KEY(user_id,profile_id)
);
CREATE TABLE persona_observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 profile_id TEXT NOT NULL,
 turn_id TEXT NOT NULL UNIQUE REFERENCES turns(id) ON DELETE CASCADE,
 message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
 created_at INTEGER NOT NULL
);
CREATE INDEX persona_observations_scope ON persona_observations(user_id,profile_id,id);
CREATE TABLE persona_traits (
 id TEXT PRIMARY KEY,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 profile_id TEXT NOT NULL,
 trait_key TEXT NOT NULL,
 kind TEXT NOT NULL,
 name TEXT NOT NULL,
 content TEXT NOT NULL,
 locked INTEGER NOT NULL DEFAULT 0,
 source TEXT NOT NULL,
 evidence TEXT NOT NULL DEFAULT '[]',
 created_at INTEGER NOT NULL,
 updated_at INTEGER NOT NULL,
 deleted_at INTEGER,
 UNIQUE(user_id,profile_id,trait_key)
);
