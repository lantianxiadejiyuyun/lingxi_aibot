"""Read-only legacy database export for the Rust importer; does not start Flask.

Use an isolated database account with SELECT permission. Stop legacy writers while
exporting; copy data/images separately. The output contains private data/secrets.
"""
import argparse
import base64
from datetime import date, datetime
from decimal import Decimal
import json
import os
from pathlib import Path

from sqlalchemy import MetaData, create_engine, select


def encode(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"Unsupported export type: {type(value).__name__}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    url = os.environ.get("LINGXI_LEGACY_DATABASE_URL")
    if not url:
        parser.error("set LINGXI_LEGACY_DATABASE_URL (SQLAlchemy database URL)")
    engine = create_engine(url)
    metadata = MetaData()
    with engine.connect() as connection, connection.begin():
        metadata.reflect(bind=connection)
        tables = {
            table.name: [dict(row._mapping) for row in connection.execute(select(table))]
            for table in metadata.sorted_tables
        }
    # Exclusive creation prevents overwriting another snapshot.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump({"format": "lingxi-legacy-v1", "tables": tables}, file,
                  ensure_ascii=False, default=encode)
    print(f"Exported {len(tables)} tables, {sum(map(len, tables.values()))} rows.")


if __name__ == "__main__":
    main()
