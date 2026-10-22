CREATE TABLE users (
 id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password_hash TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
 is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
 created_at INTEGER NOT NULL
);
CREATE TABLE tokens (
 hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name TEXT NOT NULL, expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX tokens_owner ON tokens(user_id);
CREATE TABLE settings (
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 key TEXT NOT NULL, value TEXT NOT NULL CHECK(json_valid(value)),
 PRIMARY KEY(user_id, key)
);
CREATE TABLE records (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
 data TEXT NOT NULL CHECK(json_valid(data)), status TEXT NOT NULL DEFAULT 'open',
 due_at INTEGER, revision INTEGER NOT NULL DEFAULT 1,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER
);
CREATE INDEX records_list ON records(user_id, kind, deleted_at, updated_at DESC, id);
CREATE INDEX records_due ON records(user_id, kind, status, due_at) WHERE deleted_at IS NULL;
CREATE VIRTUAL TABLE records_fts USING fts5(id UNINDEXED, title, content, tokenize='trigram');
CREATE TRIGGER records_insert AFTER INSERT ON records WHEN new.deleted_at IS NULL BEGIN
 INSERT INTO records_fts(id,title,content) VALUES(new.id,new.title,new.content);
END;
CREATE TRIGGER records_update AFTER UPDATE ON records BEGIN
 DELETE FROM records_fts WHERE id=old.id;
 INSERT INTO records_fts(id,title,content) SELECT new.id,new.title,new.content WHERE new.deleted_at IS NULL;
END;
CREATE TRIGGER records_delete AFTER DELETE ON records BEGIN
 DELETE FROM records_fts WHERE id=old.id;
END;
CREATE TABLE memories (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 fact_key TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('manual','auto')),
 importance INTEGER NOT NULL CHECK(importance BETWEEN 1 AND 5), expires_at INTEGER,
 provenance TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(provenance)),
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER,
 UNIQUE(user_id, fact_key)
);
CREATE INDEX memories_context ON memories(user_id, deleted_at, importance DESC, updated_at DESC);
CREATE TABLE conversations (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', through_id INTEGER NOT NULL DEFAULT 0,
 profile_id TEXT, lease_token TEXT, lease_until INTEGER,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX conversations_list ON conversations(user_id,updated_at DESC);
CREATE TABLE messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')), content TEXT NOT NULL,
 tool_calls TEXT, created_at INTEGER NOT NULL
);
CREATE INDEX messages_context ON messages(conversation_id,id);
CREATE TABLE turns (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 request_id TEXT NOT NULL, input_hash TEXT NOT NULL, status TEXT NOT NULL,
 reply TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, finished_at INTEGER,
 UNIQUE(user_id, request_id)
);
CREATE TABLE tool_steps (
 turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE, call_id TEXT NOT NULL,
 name TEXT NOT NULL, arguments TEXT NOT NULL, result TEXT, status TEXT NOT NULL,
 PRIMARY KEY(turn_id,call_id)
);
CREATE TABLE schedules (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 name TEXT NOT NULL, action TEXT NOT NULL, params TEXT NOT NULL CHECK(json_valid(params)),
 cron TEXT NOT NULL, timezone TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
 next_run_at INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX schedules_due ON schedules(enabled,next_run_at);
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 action TEXT NOT NULL, params TEXT NOT NULL CHECK(json_valid(params)),
 status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 3, run_at INTEGER NOT NULL,
 lease_token TEXT, lease_until INTEGER, cancel_requested INTEGER NOT NULL DEFAULT 0,
 dedupe_key TEXT, result TEXT, error TEXT NOT NULL DEFAULT '',
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, UNIQUE(user_id,dedupe_key)
);
CREATE INDEX jobs_due ON jobs(status,run_at,lease_until);
CREATE INDEX jobs_owner ON jobs(user_id,created_at DESC);
CREATE TABLE notifications (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 title TEXT NOT NULL, body TEXT NOT NULL, read_at INTEGER, created_at INTEGER NOT NULL,
 dedupe_key TEXT, UNIQUE(user_id,dedupe_key)
);
CREATE INDEX notifications_owner ON notifications(user_id,created_at DESC);
CREATE TABLE embeddings (
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 source_id TEXT NOT NULL, source_kind TEXT NOT NULL, model TEXT NOT NULL,
 content_hash TEXT NOT NULL, vector TEXT NOT NULL CHECK(json_valid(vector)),
 PRIMARY KEY(user_id,source_kind,source_id)
);
CREATE TABLE channel_bindings (
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 channel TEXT NOT NULL, external_id TEXT NOT NULL,
 conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
 PRIMARY KEY(user_id,channel,external_id)
);
CREATE TABLE import_log (source TEXT PRIMARY KEY, imported_at INTEGER NOT NULL);
