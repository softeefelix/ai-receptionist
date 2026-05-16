#!/usr/bin/env python3
"""One-time migration: create tables and grant permissions to claude_reporting."""
import os
import sys
from pathlib import Path

def load_env(path):
    result = {}
    p = Path(path)
    if not p.exists():
        return result
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, _, v = line.partition('=')
            result[k.strip()] = v.strip()
    return result

env = load_env(Path.home() / 'softeedashboard/.env')
env.update(load_env(Path(__file__).parent / '.env'))

DB_ADMIN_URL = env.get('DB_ADMIN_URL', '')
if not DB_ADMIN_URL:
    print('DB_ADMIN_URL not found'); sys.exit(1)

import psycopg2
conn = psycopg2.connect(DB_ADMIN_URL)
cur  = conn.cursor()

cur.execute("""
    CREATE TABLE IF NOT EXISTS processed_calls (
        call_id TEXT PRIMARY KEY,
        processed_at BIGINT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS poll_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS shadow_log_entries (
        id SERIAL PRIMARY KEY,
        call_id TEXT,
        from_number TEXT,
        ts BIGINT,
        prod_node TEXT,
        staging_node TEXT,
        diff BOOLEAN,
        routed_to TEXT,
        route_reason TEXT,
        sentiment TEXT,
        summary TEXT,
        caller_message TEXT,
        caller_email TEXT,
        logged_at BIGINT
    );
    CREATE TABLE IF NOT EXISTS jobber_tokens (
        id INTEGER PRIMARY KEY,
        tokens_json TEXT NOT NULL,
        updated_at BIGINT NOT NULL
    );
    GRANT ALL ON processed_calls, poll_metadata, shadow_log_entries, jobber_tokens
        TO claude_reporting;
    GRANT USAGE, SELECT ON SEQUENCE shadow_log_entries_id_seq TO claude_reporting;
""")
conn.commit()
print('Schema created and permissions granted to claude_reporting.')
conn.close()
