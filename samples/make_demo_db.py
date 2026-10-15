#!/usr/bin/env python3
"""Build samples/demo_store.db — a throwaway SQLite store with a generic
schema (an `items` table with a `source` column), seeded with a long-tail
source distribution so the reversible lifecycle can be proven on a
real-schema store that is safe to mutate."""
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "demo_store.db")

# A synthetic long tail: a few dominant sources + many rare one-offs.
SOURCES = [
    ("api", 200), ("cron", 60), ("task", 15), ("manual", 8),
    ("migration-2026-01", 13), ("directive", 4), ("import-batch", 3),
    ("correction", 3), ("identity", 2), ("user", 2), ("tool", 1),
    ("kernel-doc", 1), ("insight", 1), ("fact", 1), ("preference", 1),
    ("rulings-walk", 1), ("incident", 1), ("context-sync", 1),
    ("partner:relay:2026-09-10", 1), ("canon", 1), ("decision", 1),
]

if os.path.exists(OUT):
    os.remove(OUT)
conn = sqlite3.connect(OUT)
conn.executescript(
    """
    CREATE TABLE items (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        source TEXT,
        timestamp TEXT,
        session_id TEXT DEFAULT 'default',
        importance REAL DEFAULT 0.5,
        metadata_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        memory_type TEXT DEFAULT 'unknown',
        valid_until TIMESTAMP DEFAULT NULL,
        superseded_by TEXT DEFAULT NULL
    );
    """
)
n = 0
for src, count in SOURCES:
    for i in range(count):
        n += 1
        conn.execute(
            "INSERT INTO items (id, content, source, memory_type, metadata_json) "
            "VALUES (?,?,?,?,?)",
            ("m-%04d" % n, "%s item %d" % (src, i + 1), src, "context", "{}"),
        )
conn.commit()
print("wrote %d rows to %s across %d distinct sources" % (n, OUT, len(SOURCES)))
conn.close()
