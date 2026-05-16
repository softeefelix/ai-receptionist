#!/usr/bin/env python3
"""
Backfill shadow_log.jsonl with calls from a past date or range.
Reads from Retell, classifies, and shadow-logs — does NOT route
(no emails sent, no Jobber requests created).

Usage:
    python3 backfill_shadow.py              # yesterday
    python3 backfill_shadow.py 2026-05-15   # specific date
    python3 backfill_shadow.py 2026-05-10 2026-05-15  # range (inclusive)
"""

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# Reuse everything from main.py — env loading, API calls, shadow_log, classify_call
sys.path.insert(0, str(Path(__file__).parent))
import main as _m


# ── Helpers ───────────────────────────────────────────────────────────────────

def _existing_logged_ids():
    if not _m.SHADOW_LOG_FILE.exists():
        return set()
    ids = set()
    for line in _m.SHADOW_LOG_FILE.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                ids.add(json.loads(line)['call_id'])
            except (json.JSONDecodeError, KeyError):
                pass
    return ids


def _local_day_bounds_ms(d):
    """Return (start_ms, end_ms) for a calendar date in local time."""
    tz = datetime.now().astimezone().tzinfo
    start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)
    end   = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=tz)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _fetch_calls_in_range(start_ms, end_ms):
    """Fetch all ended calls whose start_timestamp falls in [start_ms, end_ms]."""
    calls          = []
    pagination_key = None

    while True:
        body = {
            'filter_criteria': {
                # use gt on (start_ms - 1) — the 'gt' operator is confirmed to work
                'start_timestamp': {'type': 'number', 'op': 'gt', 'value': start_ms - 1},
            },
            'sort_order': 'ascending',
            'limit': 100,
        }
        if pagination_key:
            body['pagination_key'] = pagination_key

        data = json.dumps(body).encode()
        req  = urllib.request.Request(
            'https://api.retellai.com/v3/list-calls',
            data=data,
            headers={
                'Authorization': f'Bearer {_m.RETELL_API_KEY}',
                'Content-Type':  'application/json',
            },
        )
        result = json.loads(urllib.request.urlopen(req).read())
        items  = result.get('items', [])

        for c in items:
            ts = c.get('start_timestamp', 0)
            if ts > end_ms:
                return calls          # past the end of our range — stop
            if c.get('call_status') == 'ended':
                calls.append(c)

        pagination_key = result.get('pagination_key')
        if not pagination_key or len(items) < 100:
            break

    return calls


def _parse_args(args):
    today = date.today()
    if not args:
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    if len(args) == 1:
        d = date.fromisoformat(args[0])
        return d, d
    return date.fromisoformat(args[0]), date.fromisoformat(args[1])


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    raw_args = [a for a in sys.argv[1:] if not a.startswith('-')]

    try:
        start_date, end_date = _parse_args(raw_args)
    except ValueError as e:
        print(f'Bad date argument: {e}')
        print('Usage: python3 backfill_shadow.py [YYYY-MM-DD] [YYYY-MM-DD]')
        raise SystemExit(1)

    start_ms = _local_day_bounds_ms(start_date)[0]
    end_ms   = _local_day_bounds_ms(end_date)[1]

    label = str(start_date) if start_date == end_date else f'{start_date} → {end_date}'
    print(f'\nBackfill: {label}')
    print(f'  Window: {datetime.fromtimestamp(start_ms/1000).strftime("%Y-%m-%d %H:%M")} – '
          f'{datetime.fromtimestamp(end_ms/1000).strftime("%Y-%m-%d %H:%M")} local')

    already_logged = _existing_logged_ids()
    print(f'  Already in shadow log: {len(already_logged)}')

    print('  Fetching calls from Retell...')
    calls     = _fetch_calls_in_range(start_ms, end_ms)
    new_calls = [c for c in calls if c.get('call_id') not in already_logged]
    print(f'  Found {len(calls)} ended call(s) in range, {len(new_calls)} not yet logged')

    if not new_calls:
        print('\n  Nothing new to log.\n')
        return

    logged  = 0
    skipped = 0

    for c in new_calls:
        call_id = c.get('call_id', '')
        try:
            full = _m.retell_get_call(call_id)
        except Exception as e:
            print(f'  [skip] {call_id} — fetch error: {e}')
            skipped += 1
            continue

        if not full.get('call_analysis'):
            print(f'  [skip] {call_id} — call_analysis not ready')
            skipped += 1
            continue

        action, reason = _m.classify_call(full)
        _m.shadow_log(full)
        logged += 1

        ts_str   = datetime.fromtimestamp((full.get('start_timestamp', 0)) / 1000).strftime('%H:%M')
        frm      = full.get('from_number') or '?'
        print(f'  [{ts_str}] {frm:<15} → {action:<8}  {reason}')

    print(f'\n  Logged: {logged}   Skipped: {skipped}')
    if logged:
        print(f'\n  Next: python3 review_shadow.py --annotate\n')
    else:
        print()


if __name__ == '__main__':
    main()
