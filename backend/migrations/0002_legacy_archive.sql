-- Original rows are encrypted, including settings and retired integration state.
CREATE TABLE legacy_archive (
 source TEXT NOT NULL, table_name TEXT NOT NULL, row_id TEXT NOT NULL, ciphertext TEXT NOT NULL,
 PRIMARY KEY(source,table_name,row_id)
);
