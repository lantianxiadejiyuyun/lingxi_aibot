-- Owner QQ attachments belong to the main database. Sources and original image
-- bytes are encrypted together with the account master key, never in group memory.
ALTER TABLE qq_private_events ADD COLUMN media TEXT NOT NULL DEFAULT '';
