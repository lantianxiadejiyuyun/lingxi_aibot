-- Only outbound identifiers and timestamps; no message contents or image bytes.
CREATE TABLE sticker_reply_turns (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id TEXT NOT NULL, scope TEXT NOT NULL, delivery_key TEXT NOT NULL,
 text_at INTEGER NOT NULL, sticker_id TEXT, image_at INTEGER, image_turn INTEGER,
 UNIQUE(user_id,scope,delivery_key)
);
CREATE INDEX sticker_reply_scope ON sticker_reply_turns(user_id,scope,image_at);
CREATE INDEX sticker_reply_retention ON sticker_reply_turns(text_at);
