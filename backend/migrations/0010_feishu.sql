CREATE TABLE feishu_state (
 user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 enabled INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
 token_hash TEXT UNIQUE, profile_id TEXT,
 owner_id TEXT NOT NULL DEFAULT '', chat_id TEXT NOT NULL DEFAULT '', binding_key TEXT NOT NULL DEFAULT '',
 conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
 pair_hash TEXT, pair_key TEXT, pair_expires_at INTEGER,
 heartbeat_at INTEGER, connection_state TEXT NOT NULL DEFAULT 'offline', sdk_version TEXT NOT NULL DEFAULT '',
 spool_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE feishu_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 message_id TEXT NOT NULL, config_key TEXT NOT NULL, owner_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 text TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 reply TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 UNIQUE(user_id,message_id)
);
CREATE INDEX feishu_inbox ON feishu_events(user_id,status,id);
CREATE TABLE feishu_parts (
 event_id INTEGER NOT NULL REFERENCES feishu_events(id) ON DELETE CASCADE,
 part INTEGER NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', receipt TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(event_id,part)
);
