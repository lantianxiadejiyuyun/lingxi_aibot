CREATE TABLE bili_state (
 user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 config TEXT NOT NULL CHECK(json_valid(config)), revision INTEGER NOT NULL DEFAULT 1,
 credential TEXT NOT NULL DEFAULT '', account_name TEXT NOT NULL DEFAULT '', next_run_at INTEGER NOT NULL
);
CREATE TABLE bili_runs (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id), request_key TEXT NOT NULL,
 mode TEXT NOT NULL, input TEXT NOT NULL CHECK(json_valid(input)), config TEXT NOT NULL CHECK(json_valid(config)),
 revision INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued', progress TEXT NOT NULL DEFAULT '等待阅读',
 report TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 UNIQUE(user_id,request_key)
);
CREATE UNIQUE INDEX bili_one_active ON bili_runs(user_id) WHERE status IN ('queued','running');
CREATE INDEX bili_runs_owner ON bili_runs(user_id,created_at DESC);
CREATE TABLE bili_entries (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 run_id TEXT NOT NULL REFERENCES bili_runs(id), bvid TEXT NOT NULL, page INTEGER NOT NULL,
 title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '', duration INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'reading', reason TEXT NOT NULL DEFAULT '',
 source TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT '',
 segments TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(segments)),
 analysis TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(analysis)), favorite INTEGER NOT NULL DEFAULT 0,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, UNIQUE(user_id,bvid,page)
);
CREATE INDEX bili_entries_owner ON bili_entries(user_id,updated_at DESC);
CREATE TABLE bili_usage (
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, day TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,day)
);
CREATE TABLE bili_login (
 user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 id TEXT NOT NULL, secret TEXT NOT NULL, expires_at INTEGER NOT NULL, polled_at INTEGER NOT NULL DEFAULT 0
);
