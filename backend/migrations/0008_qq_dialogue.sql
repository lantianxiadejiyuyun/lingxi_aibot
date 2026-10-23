CREATE TABLE qq_reply_parts (
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 delivery_key TEXT NOT NULL, position INTEGER NOT NULL, body TEXT NOT NULL,
 status TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 PRIMARY KEY(user_id,delivery_key,position)
);
CREATE TABLE qq_outreach (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 group_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, origin TEXT NOT NULL,
 root TEXT NOT NULL DEFAULT '', step INTEGER NOT NULL DEFAULT 1,
 anchor TEXT NOT NULL, config_key TEXT NOT NULL, due_at INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', body TEXT NOT NULL DEFAULT '',
 reason TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 UNIQUE(user_id,group_id,kind,origin)
);
CREATE INDEX qq_outreach_due ON qq_outreach(status,due_at);
