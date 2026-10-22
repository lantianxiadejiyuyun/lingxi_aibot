-- Only the owner's QQ messages belong to the main database.
-- Other members' messages and memory metadata live in qq/people.sqlite3.
CREATE TABLE qq_owner_events (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 group_id TEXT NOT NULL, qq_id TEXT NOT NULL, name TEXT NOT NULL,
 content TEXT NOT NULL, triggered INTEGER NOT NULL, occurred_at INTEGER NOT NULL,
 created_at INTEGER NOT NULL, reply TEXT NOT NULL DEFAULT '',
 delivery_status TEXT NOT NULL DEFAULT 'pending',
 conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
 message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL
);
CREATE INDEX qq_owner_history ON qq_owner_events(user_id,group_id,occurred_at);
CREATE TABLE qq_reply_limits (
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 group_id TEXT NOT NULL, last_at INTEGER NOT NULL,
 PRIMARY KEY(user_id,group_id)
);
