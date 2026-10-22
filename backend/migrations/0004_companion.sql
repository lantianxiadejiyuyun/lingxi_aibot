CREATE TABLE companion_state (
 user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 config TEXT NOT NULL CHECK(json_valid(config)), revision INTEGER NOT NULL DEFAULT 1,
 next_check_at INTEGER NOT NULL, last_attempt_at INTEGER, last_sent_at INTEGER,
 lease_token TEXT, lease_until INTEGER,
 conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL
);
CREATE TABLE companion_activity (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 status TEXT NOT NULL, reason TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
 sources TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(sources)),
 fingerprint TEXT, message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 UNIQUE(user_id,fingerprint)
);
CREATE INDEX companion_activity_owner ON companion_activity(user_id,created_at DESC);
CREATE TABLE companion_delivery (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 activity_id TEXT NOT NULL REFERENCES companion_activity(id) ON DELETE CASCADE,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 channel TEXT NOT NULL, target TEXT NOT NULL, config_digest TEXT NOT NULL,
 revision INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '',
 lease_token TEXT, lease_until INTEGER, expires_at INTEGER NOT NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 UNIQUE(activity_id,channel)
);
CREATE INDEX companion_delivery_owner ON companion_delivery(user_id,channel,status);
CREATE TABLE private_bindings (
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, channel TEXT NOT NULL,
 external_id TEXT NOT NULL, conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
 updated_at INTEGER NOT NULL, PRIMARY KEY(user_id,channel), UNIQUE(channel,external_id)
);
CREATE TABLE qq_private_events (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 content TEXT NOT NULL, reply TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
 bot_qq TEXT NOT NULL, owner_qq TEXT NOT NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
