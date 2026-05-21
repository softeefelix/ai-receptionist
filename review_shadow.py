#!/usr/bin/env python3
"""
Review shadow_log.jsonl and annotate calls with correct routing.

Usage:
    python3 review_shadow.py               # summary of all calls
    python3 review_shadow.py --tail 50     # last 50 calls only
    python3 review_shadow.py --annotate    # walk through unannotated calls interactively
    python3 review_shadow.py --mismatches  # show calls where routing disagreed with annotation

Annotations are saved to shadow_annotations.json.
Over time these reveal patterns to improve the classifier in main.py.
"""

import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo('America/Los_Angeles')

LOG_FILE        = Path(__file__).parent / 'shadow_log.jsonl'
ANNOTATION_FILE = Path(__file__).parent / 'shadow_annotations.json'

ROUTING_CHOICES = {'j': 'jobber', 'e': 'email', 'i': 'ignore', 's': None}
ROUTING_LABELS  = {'jobber': '→ Jobber', 'email': '→ Email', 'ignore': '→ Ignore'}


# ── Annotation storage ────────────────────────────────────────────────────────

def load_annotations():
    if ANNOTATION_FILE.exists():
        return json.loads(ANNOTATION_FILE.read_text())
    return {}

def save_annotations(annotations):
    ANNOTATION_FILE.write_text(json.dumps(annotations, indent=2))


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_entries(tail=None):
    if not LOG_FILE.exists():
        return []
    entries = []
    for line in LOG_FILE.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if tail:
        entries = entries[-tail:]
    return entries

def fmt_ts(ts):
    if not ts:
        return '?'
    return (datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            .astimezone(PACIFIC).strftime('%m/%d %I:%M%p'))

def fmt_ts_long(ts):
    if not ts:
        return 'unknown'
    return (datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            .astimezone(PACIFIC).strftime('%Y-%m-%d %I:%M %p %Z'))

def routing_label(r):
    return ROUTING_LABELS.get(r, r or '—')


# ── Summary view ──────────────────────────────────────────────────────────────

def cmd_summary(entries, annotations):
    if not entries:
        print('No calls logged yet.')
        return

    total      = len(entries)
    diffs      = [e for e in entries if e.get('diff')]
    sentiments = Counter((e.get('sentiment') or 'unknown').lower() for e in entries)
    routings   = Counter(e.get('routed_to') or 'unknown' for e in entries)
    annotated  = {cid: a for cid, a in annotations.items()
                  if any(e['call_id'] == cid for e in entries)}

    mismatches = [
        e for e in entries
        if e['call_id'] in annotations
        and annotations[e['call_id']]['correct_routing'] != e.get('routed_to')
    ]

    print(f'\n{"="*64}')
    print(f'  Shadow Log  —  {total} call{"s" if total != 1 else ""}')
    print(f'{"="*64}')

    print()
    print('  Routing (actual):')
    for routing, count in routings.most_common():
        bar = '█' * count
        print(f'    {routing:<10} {count:>4}  {bar}')

    print()
    print('  Sentiment:')
    for sentiment, count in sentiments.most_common():
        bar = '█' * count
        print(f'    {sentiment:<22} {count:>4}  {bar}')

    print()
    print('  Staging flow diff:')
    print(f'    Same routing:       {total - len(diffs):>4}  ({100*(total-len(diffs))//total}%)')
    print(f'    Would differ:       {len(diffs):>4}  ({100*len(diffs)//total}%)')

    if diffs:
        diff_nodes = Counter(e.get('staging_node', 'unknown') for e in diffs)
        print()
        print('  Differing calls would go to:')
        for node, count in diff_nodes.most_common():
            print(f'    {node:<38} {count}')

    print()
    print(f'  Annotations: {len(annotated)}/{total} calls reviewed')
    if mismatches:
        print(f'  ⚠  {len(mismatches)} call(s) were routed differently than annotated')
        print('     Run: python3 review_shadow.py --mismatches')

    if len(annotated) == 0:
        print()
        print('  No annotations yet. Run: python3 review_shadow.py --annotate')

    print(f'{"="*64}\n')


# ── Annotate mode ─────────────────────────────────────────────────────────────

def cmd_annotate(entries, annotations):
    unannotated = [e for e in entries if e['call_id'] not in annotations]

    if not unannotated:
        print(f'\nAll {len(entries)} calls are annotated.\n')
        return

    print(f'\n{len(unannotated)} unannotated call(s). Reviewing...')
    print('Commands: j=Jobber  e=Email  i=Ignore  s=Skip  q=Quit\n')

    saved = 0
    for e in unannotated:
        call_id = e['call_id']

        print('─' * 64)
        print(f'  {fmt_ts_long(e.get("ts"))}   from {e.get("from") or "unknown"}')
        print(f'  Routed to:   {routing_label(e.get("routed_to"))}  ({e.get("route_reason", "")})')
        print(f'  Sentiment:   {e.get("sentiment") or "—"}')

        if e.get('summary'):
            print(f'  Summary:     {e["summary"]}')
        if e.get('caller_message'):
            print(f'  Message:     {e["caller_message"]}')
        if e.get('caller_email'):
            print(f'  Email:       {e["caller_email"]}')
        if e.get('diff'):
            print(f'  Staging:     would route to "{e.get("staging_node")}"')

        print()
        while True:
            raw = input('  Correct routing [j/e/i/s/q]: ').strip().lower()
            if raw == 'q':
                save_annotations(annotations)
                print(f'\nSaved {saved} annotation(s). Exiting.\n')
                return
            if raw not in ROUTING_CHOICES:
                print('  Enter j, e, i, s, or q.')
                continue
            break

        if raw == 's':
            print('  Skipped.\n')
            continue

        correct = ROUTING_CHOICES[raw]
        note = input('  Note (optional, Enter to skip): ').strip()

        annotations[call_id] = {
            'correct_routing': correct,
            'note':            note or '',
            'actual_routing':  e.get('routed_to'),
            'annotated_at':    int(time.time()),
        }
        saved += 1

        match = '✓ correct' if correct == e.get('routed_to') else f'✗ was {routing_label(e.get("routed_to"))}'
        print(f'  Saved: {routing_label(correct)}  ({match})\n')

    save_annotations(annotations)
    print(f'Done. {saved} annotation(s) saved.\n')


# ── Mismatches view ───────────────────────────────────────────────────────────

def cmd_mismatches(entries, annotations):
    by_id = {e['call_id']: e for e in entries}
    mismatches = [
        (annotations[cid], by_id[cid])
        for cid in annotations
        if cid in by_id and annotations[cid]['correct_routing'] != by_id[cid].get('routed_to')
    ]

    if not mismatches:
        print('\nNo mismatches — classifier agrees with all annotations.\n')
        return

    print(f'\n{"="*64}')
    print(f'  Routing Mismatches  —  {len(mismatches)} call(s)')
    print(f'{"="*64}')

    pattern_counter = Counter()
    for ann, e in mismatches:
        actual  = e.get('routed_to') or 'unknown'
        correct = ann['correct_routing']
        pattern_counter[f'{actual} → should be {correct}'] += 1

    print()
    print('  Patterns:')
    for pattern, count in pattern_counter.most_common():
        print(f'    {pattern:<40} {count}x')

    print()
    print('  Detail:')
    for ann, e in mismatches:
        actual  = routing_label(e.get('routed_to'))
        correct = routing_label(ann['correct_routing'])
        print(f'  [{fmt_ts(e.get("ts"))}] {e.get("from","?"):<15} '
              f'actual={actual:<12} correct={correct}')
        if ann.get('note'):
            print(f'    Note: {ann["note"]}')
        preview = (e.get('caller_message') or e.get('summary') or '')[:70]
        if preview:
            print(f'    {preview}')
        print()

    print(f'{"="*64}')
    print()
    print('  Use these patterns to refine _BOOKING_KEYWORDS and classify_call()')
    print('  in main.py.\n')


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    tail = None

    if '--tail' in args:
        idx = args.index('--tail')
        try:
            tail = int(args[idx + 1])
        except (IndexError, ValueError):
            print('Usage: --tail N requires an integer')
            return

    entries     = load_entries(tail)
    annotations = load_annotations()

    if not entries:
        print('\nNo shadow_log.jsonl found — no calls have been logged yet.\n')
        return

    if '--annotate' in args:
        cmd_annotate(entries, annotations)
    elif '--mismatches' in args:
        cmd_mismatches(entries, annotations)
    else:
        cmd_summary(entries, annotations)


if __name__ == '__main__':
    main()
