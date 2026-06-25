#!/usr/bin/env python3
"""AI Receptionist poller — run every 5 minutes, 7 AM–10 PM PT.

Local cron entry (laptop):
    */5 7-22 * * * /Users/felixtarnarider/bin/ai-receptionist-poll.sh

Cloud (Render cron job):
    Schedule: */5 * * * *  (operating-hours check handled in code)
    Set DB_URL env var to enable PostgreSQL-backed state.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo('America/Los_Angeles')


# ── Config ────────────────────────────────────────────────────────────────────

def _load_dotenv():
    env_file = Path(__file__).parent / '.env'
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                if key.strip() not in os.environ:
                    os.environ[key.strip()] = val.strip().strip('"\'')

_load_dotenv()

RETELL_API_KEY       = os.environ['RETELL_API_KEY']
AGENTMAIL_API_KEY    = os.environ['AGENTMAIL_API_KEY']
AGENTMAIL_INBOX      = os.environ.get('AGENTMAIL_INBOX', 'mistersoftee-norcal@agentmail.to')
NOTIFY_EMAIL         = os.environ.get('NOTIFY_EMAIL', 'felix@mistersofteenorcal.com')
JOBBER_CLIENT_ID     = os.environ['JOBBER_CLIENT_ID']
JOBBER_CLIENT_SECRET = os.environ['JOBBER_CLIENT_SECRET']
JOBBER_TOKENS_FILE   = Path(__file__).parent / 'jobber_tokens.json'
JOBBER_GQL           = 'https://api.getjobber.com/api/graphql'
JOBBER_API_VERSION   = '2026-02-17'
JOBBER_TOKEN_URL     = 'https://api.getjobber.com/api/oauth/token'
STATE_FILE           = Path(__file__).parent / 'state.json'
FAILED_FILE          = Path(__file__).parent / 'failed_routes.jsonl'
PROD_AGENT_ID        = 'agent_ccf0da35e33729da19de79c464'   # new flow (promoted from staging)
STAGING_AGENT_ID     = 'agent_ccf0da35e33729da19de79c464'   # same until a new staging agent exists
SHADOW_LOG_FILE      = Path(__file__).parent / 'shadow_log.jsonl'
PROCESSED_TTL_MS          = 48 * 60 * 60 * 1000   # prune processed IDs older than 48 hours
SLACK_WEBHOOK_URL         = os.environ.get('SLACK_WEBHOOK_URL', '')
REPEAT_CALLER_THRESHOLD   = 3
REPEAT_CALLER_WINDOW_MS   = 4 * 60 * 60 * 1000   # look back 4 hours
REPEAT_CALLER_ALERT_TTL_MS = 24 * 60 * 60 * 1000  # re-alert cooldown per number
DB_URL               = os.environ.get('DB_URL', '')   # Render PostgreSQL; enables cloud-safe state

# Staff names — when a caller mentions any of these, the call can only be
# routed to email so a human can forward it to that person. No Jobber request.
KNOWN_NAMES = {
    'chelsey', 'kelsey', 'kelsey m',  # caller "Chelsey" → email only
}


# ── Database (optional — local file fallback when DB_URL is unset) ────────────

_db_connection = None

def _get_db():
    global _db_connection
    if _db_connection is not None:
        return _db_connection
    if not DB_URL:
        return None
    try:
        import psycopg2
        _db_connection = psycopg2.connect(DB_URL)
        _ensure_schema(_db_connection)
        return _db_connection
    except Exception as e:
        print(f'[DB] Could not connect: {e}')
        return None

def _ensure_schema(db):
    cur = db.cursor()
    try:
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
        """)
        db.commit()
    except Exception as e:
        # claude_reporting lacks CREATE privilege — tables created by migrate_db.py
        db.rollback()
        print(f'[DB] Schema init skipped (tables pre-exist): {e}')


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    db = _get_db()
    if db:
        cur = db.cursor()
        cur.execute("SELECT key, value FROM poll_metadata WHERE key IN ('last_run_at', 'repeat_alerts')")
        meta = {row[0]: row[1] for row in cur.fetchall()}
        last_run_at   = int(meta['last_run_at']) if 'last_run_at' in meta else int(time.time() * 1000) - 300_000
        repeat_alerts = json.loads(meta['repeat_alerts']) if 'repeat_alerts' in meta else {}
        cutoff = int(time.time() * 1000) - PROCESSED_TTL_MS
        cur.execute("SELECT call_id, processed_at FROM processed_calls WHERE processed_at > %s", (cutoff,))
        processed_ids = {row[0]: row[1] for row in cur.fetchall()}
        return {'last_run_at': last_run_at, 'processed_ids': processed_ids, 'repeat_alerts': repeat_alerts}

    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        # Migrate: old format stored processed_ids as a flat list
        if isinstance(state.get('processed_ids'), list):
            now = int(time.time() * 1000)
            state['processed_ids'] = {cid: now for cid in state['processed_ids']}
        return state
    return {'last_run_at': int(time.time() * 1000) - 300_000, 'processed_ids': {}}

def save_state(state):
    now    = int(time.time() * 1000)
    cutoff = now - PROCESSED_TTL_MS
    db = _get_db()
    if db:
        cur = db.cursor()
        cur.execute("""
            INSERT INTO poll_metadata (key, value) VALUES ('last_run_at', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (str(state.get('last_run_at', now)),))
        cur.execute("""
            INSERT INTO poll_metadata (key, value) VALUES ('repeat_alerts', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (json.dumps(state.get('repeat_alerts', {})),))
        for call_id, ts in state.get('processed_ids', {}).items():
            if ts > cutoff:
                cur.execute("""
                    INSERT INTO processed_calls (call_id, processed_at) VALUES (%s, %s)
                    ON CONFLICT (call_id) DO NOTHING
                """, (call_id, ts))
        cur.execute("DELETE FROM processed_calls WHERE processed_at <= %s", (cutoff,))
        db.commit()
        return

    state['processed_ids'] = {
        cid: ts for cid, ts in state.get('processed_ids', {}).items() if ts > cutoff
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Dead-letter queue ─────────────────────────────────────────────────────────

def load_failed():
    if not FAILED_FILE.exists():
        return []
    entries = []
    for line in FAILED_FILE.read_text().splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries

def save_failed(entries):
    if entries:
        FAILED_FILE.write_text('\n'.join(json.dumps(e) for e in entries) + '\n')
    elif FAILED_FILE.exists():
        FAILED_FILE.unlink()


# ── Retell ────────────────────────────────────────────────────────────────────

def retell_list_calls(since_ts):
    data = json.dumps({
        'filter_criteria': {
            'start_timestamp': {'type': 'number', 'op': 'gt', 'value': since_ts},
        },
        'sort_order': 'ascending',
        'limit': 100,
    }).encode()
    req = urllib.request.Request(
        'https://api.retellai.com/v3/list-calls',
        data=data,
        headers={
            'Authorization': f'Bearer {RETELL_API_KEY}',
            'Content-Type': 'application/json',
        },
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read()).get('items', [])

def retell_get_call(call_id):
    req = urllib.request.Request(
        f'https://api.retellai.com/v2/get-call/{call_id}',
        headers={'Authorization': f'Bearer {RETELL_API_KEY}'},
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


# ── Jobber ────────────────────────────────────────────────────────────────────

# Single-refresher architecture (Jun 2026): the softeedashboard Render web
# service is the ONLY process that refreshes the shared 'jobber_tokens' row
# (Jobber rotates refresh tokens — concurrent refreshers kill each other's
# chain). This poller is a READ-ONLY consumer: when the row looks stale it
# POSTs the dashboard's refresh endpoint and re-reads. Only if the dashboard
# is unreachable does it refresh itself, under the same Postgres advisory
# lock the dashboard uses, with a post-lock re-read so it never double-fires.
DASHBOARD_REFRESH_URL   = 'https://softeedashboard.onrender.com/api/jobber/refresh-token'
JOBBER_REFRESH_LOCK_KEY = 727561001  # must match softeedashboard server.py

def _jobber_load_tokens():
    # Shared canonical source: softy_dashboard_app_kv key='jobber_tokens'.
    db = _get_db()
    if db:
        cur = db.cursor()
        cur.execute("SELECT value FROM softy_dashboard_app_kv WHERE key = 'jobber_tokens_fleet'")
        row = cur.fetchone()
        if row:
            return json.loads(row[0])
        # No row yet — fall through to seed from local file or env var

    if JOBBER_TOKENS_FILE.exists():
        return json.loads(JOBBER_TOKENS_FILE.read_text())
    env_json = os.environ.get('JOBBER_TOKENS_JSON')
    if env_json:
        return json.loads(env_json)
    return None

def _jobber_save_tokens(tokens):
    tokens['saved_at'] = time.time()
    db = _get_db()
    if db:
        cur = db.cursor()
        cur.execute("""
            INSERT INTO softy_dashboard_app_kv (key, value, updated_at)
            VALUES ('jobber_tokens_fleet', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (json.dumps(tokens),))
        db.commit()
    else:
        JOBBER_TOKENS_FILE.write_text(json.dumps(tokens, indent=2))

def _tokens_are_fresh(tokens):
    # Trust saved_at + expires_in (fallback 1h) so rows missing expires_at don't
    # force a refresh on every call. Mirrors softeedashboard's logic.
    saved_at   = tokens.get('saved_at', 0)
    expires_in = tokens.get('expires_in') or 3600
    return saved_at and (time.time() - saved_at) < (expires_in - 300)

def _poke_dashboard_refresh(force=False):
    """Ask the dashboard (the single refresher) to refresh the shared row.
    Returns True if the dashboard responded OK."""
    try:
        body = json.dumps({'force': force}).encode()
        req = urllib.request.Request(
            DASHBOARD_REFRESH_URL, data=body,
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read())
        print(f"[Jobber] dashboard refresh poke → {result}")
        return True
    except Exception as e:
        print(f"[Jobber] dashboard refresh poke failed: {e}")
        return False

def _locked_self_refresh(tokens):
    """LAST RESORT (dashboard unreachable): refresh under the shared Postgres
    advisory lock. Re-reads the row after acquiring the lock so a refresh that
    happened while we waited is used instead of double-firing."""
    db = _get_db()
    lock_cur = None
    if db:
        lock_cur = db.cursor()
        lock_cur.execute('SELECT pg_advisory_lock(%s)', (JOBBER_REFRESH_LOCK_KEY,))
    try:
        reloaded = _jobber_load_tokens() or tokens
        if reloaded.get('saved_at') != tokens.get('saved_at'):
            return reloaded['access_token']  # someone else refreshed while we waited
        tokens = reloaded
        print('[Jobber] dashboard unreachable — locked self-refresh (last resort)')
        data = urllib.parse.urlencode({
            'client_id':     tokens.get('client_id')     or JOBBER_CLIENT_ID,
            'client_secret': tokens.get('client_secret') or JOBBER_CLIENT_SECRET,
            'grant_type':    'refresh_token',
            'refresh_token': tokens['refresh_token'],
        }).encode()
        req = urllib.request.Request(
            JOBBER_TOKEN_URL, data=data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        resp = urllib.request.urlopen(req)
        new_tokens = json.loads(resp.read())
        if not new_tokens.get('expires_in'):
            new_tokens['expires_in'] = 3600
        new_tokens['expires_at'] = time.time() + new_tokens['expires_in'] - 60
        if 'refresh_token' not in new_tokens:
            new_tokens['refresh_token'] = tokens['refresh_token']
        new_tokens['client_id']     = tokens.get('client_id')     or JOBBER_CLIENT_ID
        new_tokens['client_secret'] = tokens.get('client_secret') or JOBBER_CLIENT_SECRET
        _jobber_save_tokens(new_tokens)
        return new_tokens['access_token']
    finally:
        if lock_cur is not None:
            try:
                lock_cur.execute('SELECT pg_advisory_unlock(%s)', (JOBBER_REFRESH_LOCK_KEY,))
            except Exception:
                pass

def get_jobber_token(force_refresh=False):
    tokens = _jobber_load_tokens()
    if not tokens:
        raise RuntimeError('No Jobber tokens — copy jobber_tokens.json from softeedashboard')

    if not force_refresh and _tokens_are_fresh(tokens):
        return tokens['access_token']

    # Stale — refresh directly under the shared advisory lock.
    # The poller shares the same Postgres DB as the dashboard, so it can
    # refresh tokens itself without an HTTP poke to a service that may
    # be cold-starting. The lock prevents races with the dashboard.
    return _locked_self_refresh(tokens)

def jobber_query(query, variables=None, _attempt=0):
    token = get_jobber_token()
    data = json.dumps({'query': query, 'variables': variables or {}}).encode()
    req = urllib.request.Request(JOBBER_GQL, data=data, headers={
        'Authorization':            f'Bearer {token}',
        'Content-Type':             'application/json',
        'X-JOBBER-GRAPHQL-VERSION': JOBBER_API_VERSION,
    })
    try:
        resp   = urllib.request.urlopen(req)
        result = json.loads(resp.read())
        if result.get('errors'):
            raise RuntimeError(f'Jobber GraphQL error: {result["errors"]}')
        return result
    except urllib.error.HTTPError as e:
        if e.code == 401 and _attempt == 0:
            get_jobber_token(force_refresh=True)
            return jobber_query(query, variables, _attempt=1)
        if e.code == 429 and _attempt < 3:
            time.sleep(2 ** _attempt)
            return jobber_query(query, variables, _attempt=_attempt + 1)
        raise

def search_client_by_phone(phone):
    digits = re.sub(r'\D', '', phone)
    if len(digits) < 10:
        return None
    result = jobber_query(
        '{ clients(searchTerm: "%s", first: 5) { nodes { id phones { number } } } }' % digits[-10:]
    )
    for node in result.get('data', {}).get('clients', {}).get('nodes', []):
        for p in node.get('phones', []):
            if re.sub(r'\D', '', p.get('number', ''))[-10:] == digits[-10:]:
                return node['id']
    return None

def create_client(name, phone):
    parts = (name or 'Unknown Caller').strip().split(None, 1)
    inp = {
        'firstName': parts[0],
        'lastName':  parts[1] if len(parts) > 1 else '',
        'phones':    [{'number': phone, 'description': 'MAIN', 'primary': True}],
    }
    result = jobber_query("""
        mutation($input: ClientCreateInput!) {
          clientCreate(input: $input) {
            client { id }
            userErrors { message path }
          }
        }
    """, {'input': inp})
    errors = result.get('data', {}).get('clientCreate', {}).get('userErrors', [])
    if errors:
        raise RuntimeError(f'clientCreate errors: {errors}')
    return result['data']['clientCreate']['client']['id']

def create_jobber_request(client_id, title, overview):
    result = jobber_query("""
        mutation($input: RequestCreateInput!) {
          requestCreate(input: $input) {
            request { id title }
            userErrors { message path }
          }
        }
    """, {'input': {'clientId': client_id, 'title': title,
                    'assessment': {'instructions': overview}}})
    errors = result.get('data', {}).get('requestCreate', {}).get('userErrors', [])
    if errors:
        raise RuntimeError(f'requestCreate errors: {errors}')
    return result['data']['requestCreate']['request']


def create_jobber_note(request_id, message):
    result = jobber_query("""
        mutation($requestId: EncodedId!, $input: RequestCreateNoteInput!) {
          requestCreateNote(requestId: $requestId, input: $input) {
            requestNote { id }
            userErrors { message path }
          }
        }
    """, {'requestId': request_id, 'input': {'message': message}})
    errors = result.get('data', {}).get('requestCreateNote', {}).get('userErrors', [])
    if errors:
        raise RuntimeError(f'requestCreateNote errors: {errors}')


# ── AgentMail ─────────────────────────────────────────────────────────────────

def send_email(subject, body_text, body_html=None):
    payload = {
        'to':      [NOTIFY_EMAIL],
        'subject': subject,
        'text':    body_text,
    }
    if body_html:
        payload['html'] = body_html
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f'https://api.agentmail.to/v0/inboxes/{AGENTMAIL_INBOX}/messages/send',
        data=data,
        headers={
            'Authorization': f'Bearer {AGENTMAIL_API_KEY}',
            'Content-Type':  'application/json',
        },
    )
    resp   = urllib.request.urlopen(req)
    result = json.loads(resp.read())
    print(f'[Email] Sent "{subject}" → {result.get("message_id", "?")}')

def send_slack(text):
    """POST a message to the configured Slack webhook.
    Falls back to an urgent email when SLACK_WEBHOOK_URL is not set.
    """
    if not SLACK_WEBHOOK_URL:
        send_email(f'[URGENT — Slack not configured] {text[:80]}', text)
        return
    data = json.dumps({'text': text}).encode()
    req  = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=data,
        headers={'Content-Type': 'application/json'},
    )
    urllib.request.urlopen(req)
    print(f'[Slack] Message sent')


# ── Classification ────────────────────────────────────────────────────────────

# 'party' (singular) removed — fires as false positive on "appropriate party" (meaning person).
# 'parties' (plural) kept — almost always means events in this context.
# Only caller_message is checked (not call_summary) to avoid false positives
# from free-form phrases like "transferred to the appropriate party."
_BOOKING_KEYWORDS = [
    'book', 'booking',
    'catering', 'cater', 'hire', 'rent', 'rental', 'private',
    'birthday', 'wedding', 'corporate', 'celebration',
    'reserve', 'reservation',
    'event', 'party', 'parties', 'function',
    'fundraiser', 'fund raiser', 'graduation', 'field day',
]

# Subset of _BOOKING_KEYWORDS that unambiguously signal a NEW booking, even in callback context.
# 'event' and 'reservation' are intentionally excluded — they often refer to existing ones.
_STRONG_BOOKING_KEYWORDS = {
    'book', 'booking',
    'catering', 'cater', 'hire', 'rent', 'rental', 'private',
    'birthday', 'wedding', 'corporate', 'celebration',
    'reserve', 'party', 'parties', 'function',
    'fundraiser', 'fund raiser', 'graduation', 'field day',
}
# Word-boundary version — prevents 'rent' matching 'currently', 'book' matching 'notebook', etc.
_STRONG_BOOKING_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(kw) for kw in sorted(_STRONG_BOOKING_KEYWORDS, key=len, reverse=True)) + r')\b'
)
_BOOKING_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(kw) for kw in sorted(_BOOKING_KEYWORDS, key=len, reverse=True)) + r')\b'
)

# Truck dispatch language — caller is asking for Mister Softee to come somewhere.
# Restricted to unambiguous dispatch verbs (request/send/arrange/dispatch/book/
# hire/reserve) to avoid catching location inquiries ("looking for the truck",
# "want to locate a truck") or existing-booking follow-ups.
_TRUCK_DISPATCH_RE = re.compile(
    r'\b(?:request|requesting|requests|requested|'
    r'send|sending|sends|sent|'
    r'arrange|arranging|arranges|arranged|'
    r'dispatch|dispatching|dispatches|dispatched|'
    r'book|booking|books|booked|'
    r'hire|hiring|hires|hired|'
    r'reserve|reserving|reserves|reserved)\b'
    r'(?:\s+(?!information\b|info\b|message\b|callback\b|'
    r'assistance\b|help\b|aid\b|'
    r'checking\b|check\b|find\b|finding\b|locate\b|locating\b|'
    r'track\b|tracking\b|locator\b|tracker\b)\w+){0,4}\s+'
    r'(?:truck|mister\s+softee)\b'
)

# Caller is returning a missed call — route to email unless strong booking intent is clear
_RETURNING_CALL_PHRASES = [
    'returning a call', 'returning your call', 'returning the call',
    'returning a phone call', 'returning the phone call', 'returning your phone call',
    'calling back', 'call back', 'missed call', 'received a call',
    'got a call', 'called me', 'you called', 'someone called',
]

# Caller is checking on an existing booking — email, not a new Jobber request
_EXISTING_BOOKING_VERBS = [
    'confirm', 'confirming', 'check on', 'checking on',
    'verify', 'verifying',
    'update', 'updating', 'change', 'changing', 'modify', 'modifying',
    'cancel', 'cancelling', 'canceling',
]
_BOOKING_NOUNS = ['reservation', 'booking', 'appointment', 'event']

# Caller is following up on something they already initiated — distinct from
# "follow up with her regarding the request" (agent's closure language).
# Caller asking a question about their existing reservation/booking — not a new inquiry.
# Catches "their upcoming reservation", "my booking", "our appointment", etc.
_EXISTING_RESERVATION_RE = re.compile(
    r'\b(?:their|his|her|my|our)\s+(?:upcoming\s+|existing\s+|current\s+)?'
    r'(?:reservation|booking|appointment)\b'
    r'|\bupcoming\s+(?:reservation|booking|appointment)\b'
)

# Requires the verb to govern the booking noun directly.
_EXISTING_FOLLOWUP_RE = re.compile(
    r'\b(?:following up on|followed up on|follow up on|'
    r'calling about|calling to follow up on|calling to check on)\s+'
    r'(?:a|an|the|her|his|their|my|our)\s*'
    r'(?:previous|prior|earlier|original|existing|event|booking|catering)?\s*'
    r'\b(?:request|booking|reservation|appointment|inquiry|order|email)\b'
)

# Caller explicitly left no actionable message — safe to ignore
_NO_MESSAGE_INDICATORS = [
    'did not leave', 'no specific message', 'did not provide',
    'declined to provide', 'no message left', 'did not specify',
    'did not have a clear request', 'no coherent request',
]

# Caller asking how the process works — informational inquiry, not a booking
_PROCESS_INQUIRY_PHRASES = [
    'seeking information',
    'wanted to know how',
    'inquired about how',
    'asking about how',
    'asking how to',
    'how to have a truck',
    'how to get a truck',
    'how the process',
    'information on the process',
    'information about the process',
    'how does it work',
    'how do i book',
    'how do we book',
]

# Caller asking about quantity/capacity/pricing/menu — informational, needs a
# human answer, not a Jobber ticket. Even if 'event' or other booking keywords
# appear, these signal pre-booking research.
_INFO_QUERY_PHRASES = [
    # quantity / capacity
    'how many servings', 'how many scoops', 'how many people can',
    'number of servings', 'number of scoops',
    'servings per', 'servings in an hour', 'servings in one hour',
    'servings in a hour',
    'how many trucks',
    # pricing
    'how much per', 'how much for', 'how much it costs', 'how much does it cost',
    'how much would it cost', 'how much it would cost', 'how much do you charge',
    'what does it cost', 'what is the cost', "what's the cost", 'what costs',
    'what is the price', "what's the price", 'what are the prices',
    'your pricing', 'your prices', 'your rates', 'pricing information',
    'price information',
    # menu / flavors
    'what flavors', 'what kind of ice cream', 'what kinds of ice cream',
    'what types of ice cream', "what's on the menu", 'what is the menu',
    'your menu', 'menu options', 'menu items',
]

# Caller is relaying information to someone, not initiating a new booking
_INFO_RELAY_PHRASES = [
    'received an email', 'got an email', 'got your email', 'saw your email',
    'received your email', 'sharing the information', 'sharing this information',
    'pass along', 'passing along', 'passed along',
    'share the information', 'sharing with my', 'sharing this with',
]
# Matches "let Lenny know", "let her know", etc. — directed at a named contact
_LET_KNOW_RE = re.compile(r'\blet \w+ know\b')

# Known staff names — word-boundary regex built from KNOWN_NAMES above.
# If a caller mentions any of these, the call can ONLY be an email so a
# human can forward to that person.  Never becomes a Jobber request.
_KNOWN_NAMES_PATTERN = re.compile(
    r'\b(?:' + '|'.join(re.escape(n) for n in sorted(KNOWN_NAMES, key=len, reverse=True)) + r')\b',
    re.IGNORECASE,
)

# Caller is specifically trying to reach a named person — email even if no message left
_TRYING_TO_REACH_PHRASES = [
    'trying to reach',
    'trying to get in touch',
    'called to reach',
    'calling to reach',
]

# Retell agent writes this exact string when the call was a location/app lookup
_LOCATION_INQUIRY_MARKER = 'location inquiry - directed to app'

# Booking-keyword phrases that come from the agent's own explanation of what a
# service would require, not from the caller's intent — e.g. "the agent explained
# that having a truck come to their house would require a private event booking."
# Also strips app UI labels like "public and private event locations" that appear
# when a caller describes what they see in the Mister Softee app map.
# Strip these before testing _STRONG_BOOKING_RE in location-inquiry context.
_AGENT_BOOKING_EXPLANATION_RE = re.compile(
    r'\b(?:would|will)\s+require\s+(?:a\s+)?(?:private\s+event\s+|private\s+|event\s+)?(?:booking|reservation|catering)\b'
    r'|\b(?:that|which)\s+requires?\s+(?:a\s+)?(?:private\s+event\s+|private\s+|event\s+)?(?:booking|reservation|catering)\b'
    r'|\b(?:public\s+and\s+)?private\s+event\s+locations?\b'
)

# Negated-event descriptors: the caller is asking for a truck that is NOT booked
# at an event (i.e. a public / roaming truck) — the OPPOSITE of booking intent.
# "where to find a truck that is not at a private event" is a location inquiry;
# the words "private"/"event" here are negated descriptors, not booking signal.
# Strip these before testing _STRONG_BOOKING_RE so a location lookup isn't
# misrouted to Jobber. False-positive that prompted this: Jobber Request 30703416
# / call_c810a3a4b11f4df22a89579c8c4 — caller wanted to find a truck NOT at a
# private event; "private" tripped _STRONG_BOOKING_RE and voided the location
# guard, creating a bogus Jobber request (Felix, 2026-06-13).
_NEGATED_EVENT_RE = re.compile(
    r"\b(?:not|isn't|is\s+not|aren't|are\s+not|that's\s+not|that\s+is\s+not|"
    r"which\s+is\s+not|without\s+being)\s+(?:currently\s+)?"
    r"(?:at|booked\s+(?:at|for)|reserved\s+for|part\s+of|attending)\s+"
    r"(?:a\s+|an\s+|any\s+)?(?:private\s+)?(?:event|party|function|booking)s?\b"
    r"|\bnot\s+(?:a\s+)?(?:private\s+)?(?:event|party|function|booking)\b"
)

# Caller asking where a truck is right now — real-time info that can't be
# answered later, so ignore (don't email or Jobber). Matches Retell's third-
# person summaries: "wants to know where the truck is", "wanted to know if
# the truck was on the road", "wants to know the current location", etc.
_LOCATION_INQUIRY_RE = re.compile(
    r'\b(?:wants?|wanted) to know\s+(?:'
    r'the\s+(?:current\s+)?location|'
    r'where\s+(?:the\s+|a\s+|any\s+)?(?:mister\s+softee\s+)?(?:truck|trucks)|'
    r'if\s+(?:the\s+|a\s+|any\s+)?(?:mister\s+softee\s+)?(?:truck|trucks)\s+(?:is|are|was|were|will)|'
    r'whether\s+(?:the\s+|a\s+|any\s+)?(?:mister\s+softee\s+)?(?:truck|trucks)\s+(?:is|are|was|were|will)'
    r')\b'
    r'|\b(?:asking|inquiring|inquired|asked)\s+(?:about|for)\s+(?:the\s+)?(?:current\s+)?(?:truck\s+)?location\b'
    r'|\b(?:location\s+of|where\s+to\s+find|closest|nearest)\s+(?:the\s+|a\s+|any\s+)?(?:mister\s+softee\s+)?(?:truck|trucks)\b'
    r'|\b(?:requesting\s+(?:assistance\s+)?to\s+locate|trying\s+to\s+(?:locate|find)|unable\s+to\s+(?:locate|find|see))\s+(?:the\s+|a\s+)?(?:mister\s+softee\s+)?(?:ice\s+cream\s+)?(?:truck|trucks)\b'
)

# Caller wants service "right now" from a truck they can see / that's nearby.
# Jobber requests are future-looking; immediate-intent calls go to email so
# a human can respond fast (or explain we don't do on-demand). Strong booking
# signal (birthday/wedding/etc.) still overrides — those are planned events.
_IMMEDIATE_INTENT_PHRASES = [
    'nearby truck', 'nearby mister softee',
    'a nearby truck', 'the nearby truck',
    'truck nearby', 'mister softee truck nearby',
    'see a truck', 'saw a truck', 'seeing a truck',
    'see a mister softee', 'saw a mister softee',
    'see the truck', 'saw the truck',
    'near me', 'near us', 'near here',
    'near my location', 'near our location',
    'from a nearby', 'from the nearby',
    'service from a nearby', 'service from the nearby',
    'service right now', 'service immediately',
    'right now from', 'currently nearby',
    'in the area right now', 'in the area now',
    'wanting service now', 'want service now',
    'truck right now', 'a truck right now',
]

# Caller asking us to add their area to a regular route — needs an ops callback,
# not a Jobber request. Triggers even when dispatch verbs are present.
_ROUTE_EXTENSION_PHRASES = [
    # Retell summarizes calls in third person, so include her/his/their variants
    'for our area', 'for my area', 'for her area', 'for his area', 'for their area', 'for the area',
    'in our area', 'in my area', 'in her area', 'in his area', 'in their area',
    'to our area', 'to my area', 'to her area', 'to his area', 'to their area',
    'for our neighborhood', 'for my neighborhood', 'for her neighborhood',
    'for his neighborhood', 'for their neighborhood',
    'in our neighborhood', 'in my neighborhood', 'in her neighborhood',
    'in his neighborhood', 'in their neighborhood',
    'to our neighborhood', 'to my neighborhood', 'to her neighborhood',
    'to his neighborhood', 'to their neighborhood',
    'for our block', 'for my block', 'for her block', 'for his block', 'for their block',
    'in our block', 'in my block', 'in her block', 'in his block', 'in their block',
    'to our block', 'to my block', 'to her block', 'to his block', 'to their block',
    'for our street', 'for my street', 'for her street', 'for his street', 'for their street',
    'on our street', 'on my street', 'on her street', 'on his street', 'on their street',
    'to our street', 'to my street', 'to her street', 'to his street', 'to their street',
    'add our area', 'add my area', 'add our neighborhood', 'add my neighborhood',
    'add our block', 'add my block',
    'add her area', 'add his area', 'add their area',
]

# Caller asking whether they can buy/purchase ice cream from a truck — informational.
# "private" in "at a private event" is a location descriptor, not booking intent.
_PURCHASE_INQUIRY_RE = re.compile(
    r'\bif\s+(?:they|he|she|i|we)\s+can\s+(?:purchase|buy)\b'
    r'|\bwhether\s+(?:they|he|she|i|we)\s+can\s+(?:purchase|buy)\b'
    r'|\bcan\s+(?:they|he|she|i|we)\s+(?:purchase|buy)\s+ice\s+cream\b'
)

# Caller asking for delivery — we don't offer delivery; route to email for a human to explain
_DELIVERY_RE = re.compile(r'\b(?:deliver(?:y|ies|ed)?|delivery\s+service)\b')

# Caller asking whether the truck comes to their street/neighborhood — not a booking
_NEIGHBORHOOD_INQUIRY_PHRASES = [
    'come down',
    'come through our', 'come through my', 'come through their',
    'come through her', 'come through his',
    'do you come to',
    'do you service',
    'do you go to',
    'do you cover',
    'will you be in',
    'will you be on',
    'pass through',
]

# Catches "come by our street", "come by their street", "go down my block",
# "stop by his neighborhood", "coming by their work area", etc.
# Third-person and gerund variants matter because Retell paraphrases in third person.
_NEIGHBORHOOD_INQUIRY_RE = re.compile(
    r'\b(?:come|coming|go|going|stop|stopping|drive|driving|pass|passing)\s+'
    r'(?:down|by|to|through|along|on)\s+'
    r'(?:our|my|her|his|their|the)\s+'
    r'(?:street|block|neighborhood|area|location|place|home|work)\b'
)

# Caller explicitly clarifies that a keyword they mentioned isn't the purpose of the call.
# Used to suppress strong booking overrides in route/neighborhood context.
_BOOKING_DISCLAIMER_RE = re.compile(
    r'\bclarified\s+(?:that\s+)?this\s+(?:call|inquiry)\b'
    r'|\bthis\s+(?:call|inquiry)\s+is\s+(?:not\s+)?about\b'
    r'|\bmentioned\s+.{0,30}\bbut\s+clarified\b'
)

# Agent transferred the caller or fully resolved their inquiry — no follow-up needed
_TRANSFERRED_PHRASES = [
    'was transferred',
    'were transferred',
    'transferred accordingly',
    'transferred to the',
    'transferred to an',
    'transferred the call',
    'transferred the caller',
    'call was transferred',
    'was connected to',
    'were connected to',
    'connected to an operator',
    'was relieved when',
    'were informed that',
    'was informed that',
    'informed about the app',
    'were already using',
    'was already using',
    'already using to locate',
]

# Caller's question was self-resolved via the app — no follow-up needed
_APP_RESOLUTION_PHRASES = [
    'decided to download',
    'will download the app',
    'going to download',
    'use the app for',
    'using the app for',
    'download the app for',
    'directed to the app',
    'recommended using the',
]

# Truck didn't show for a booked event — needs an immediate human response
_TRUCK_NO_SHOW_PHRASES = [
    'not yet arrived', 'has not arrived', 'had not arrived', "hasn't arrived",
    'not yet appeared', 'has not appeared', 'had not appeared', "hasn't appeared",
    'not yet shown', 'has not shown up', 'had not shown up', "hasn't shown up",
    'never arrived', 'never showed', 'did not arrive', 'did not show',
    'not there yet', 'still not here', "still hasn't",
    'not show up', 'not showed up',
]

_SAME_DAY_SIGNALS = [
    'today', 'tonight', 'this afternoon', 'this evening', 'this morning',
    'right now', 'in a few hours', 'in an hour', 'within the hour',
]


def _is_same_day_event(call):
    """True when the caller's event is today — triggers Slack escalation."""
    analysis    = call.get('call_analysis') or {}
    custom      = analysis.get('custom_analysis_data') or {}
    event_dt    = (custom.get('event_datetime') or '').lower()
    msg_lower   = (custom.get('caller_message') or '').lower()
    summary     = (analysis.get('call_summary') or '').lower()

    # If the AI extracted an event_datetime that says "today", trust it
    if any(sig in event_dt for sig in _SAME_DAY_SIGNALS):
        return True

    # Otherwise check the message + summary for same-day language alongside event intent
    combined = f'{msg_lower} {summary}'
    has_same_day = any(sig in combined for sig in _SAME_DAY_SIGNALS)
    has_event    = bool(_BOOKING_RE.search(combined))
    return has_same_day and has_event


def classify_call(call):
    """Returns ('jobber' | 'slack' | 'email' | 'ignore', reason).

    'slack' = same-day event request needing immediate attention.

    Classification improves over time via shadow_annotations.json — run
    `python3 review_shadow.py --annotate` after calls accumulate.
    """
    analysis = call.get('call_analysis') or {}
    custom   = analysis.get('custom_analysis_data') or {}

    in_voicemail    = analysis.get('in_voicemail', False)
    caller_message  = (custom.get('caller_message') or '').strip()
    caller_email    = (custom.get('caller_email') or '').strip()
    msg_lower       = caller_message.lower()
    summary_lower   = (analysis.get('call_summary') or '').lower()

    if in_voicemail:
        return 'email', 'voicemail'

    # Agent explicitly flagged this as a location/app lookup, or caller resolved it via app
    if _LOCATION_INQUIRY_MARKER in msg_lower:
        return 'ignore', 'location/app inquiry — agent handled'
    if any(phrase in msg_lower for phrase in _APP_RESOLUTION_PHRASES):
        return 'ignore', 'caller resolved inquiry via app — no follow-up needed'

    # Caller asking where a truck is right now — real-time info, can't be
    # answered after the fact. Strong booking signal still wins ("wants to
    # know the location of a wedding truck" → Jobber), but scrub agent-
    # explanation phrasing first so "would require a private event booking"
    # doesn't falsely override a genuine location inquiry.
    if _LOCATION_INQUIRY_RE.search(msg_lower):
        msg_no_explanation = _AGENT_BOOKING_EXPLANATION_RE.sub('', msg_lower)
        msg_no_explanation = _NEGATED_EVENT_RE.sub('', msg_no_explanation)
        if not _STRONG_BOOKING_RE.search(msg_no_explanation):
            return 'ignore', 'real-time truck location inquiry — no useful follow-up'

    # Known staff name → email only no matter what else is in the message.
    # Caller intent is to reach a person; that's email, not a booking.
    _name_match = _KNOWN_NAMES_PATTERN.search(msg_lower)
    if _name_match:
        return 'email', f'caller asked for known staff ({_name_match.group()}) — email only, no Jobber request'

    # Caller wants service NOW from a truck they see / a nearby truck.
    # Jobber requests are future-looking; "happening now" intent → email.
    # Strong booking signal still wins (planned events stay on Jobber).
    if (any(phrase in msg_lower for phrase in _IMMEDIATE_INTENT_PHRASES)
            and not _STRONG_BOOKING_RE.search(msg_lower)):
        return 'email', 'immediate/nearby service intent — Jobber is future-looking'

    # Same-day intent ("today", "tonight", "later today") without a specific
    # planned-event type → email. Jobber is future-looking; Slack still wins
    # for same-day calls that DO mention a strong booking type (wedding/birthday/
    # private/etc.) — those are planned events happening today.
    combined = f'{msg_lower} {summary_lower}'
    if (any(sig in combined for sig in _SAME_DAY_SIGNALS)
            and not _STRONG_BOOKING_RE.search(combined)):
        return 'email', 'same-day service intent — Jobber is future-looking'

    # No message at all
    if not caller_message:
        if caller_email:
            return 'email', 'no message but caller provided email — expecting follow-up'
        return 'ignore', 'no caller message — agent handled inquiry'

    # Caller is trying to reach someone — notify, unless they explicitly said they'll wait
    if any(phrase in msg_lower for phrase in _TRYING_TO_REACH_PHRASES):
        if 'will wait' in msg_lower or 'no one available' in msg_lower:
            return 'ignore', 'caller tried to reach but will wait — no action needed'
        return 'email', 'caller trying to reach someone — needs follow-up'

    # Caller explicitly left no actionable message
    if any(phrase in msg_lower for phrase in _NO_MESSAGE_INDICATORS):
        # Caller signaled booking/event intent but hung up before leaving details —
        # route to email so a human can call them back. Skip if the agent already
        # transferred them (the transfer check below would otherwise catch it).
        has_booking_intent = bool(_STRONG_BOOKING_RE.search(msg_lower)) or \
                             bool(re.search(r'\bevent\b', msg_lower))
        was_transferred = any(p in summary_lower for p in _TRANSFERRED_PHRASES)
        if has_booking_intent and not was_transferred:
            return 'email', 'caller expressed booking intent but left no details — needs follow-up'
        return 'ignore', 'caller left no specific message'

    # Returning a missed call — email, UNLESS strong new-booking intent is present.
    # Use word-boundary regex so 'rent' doesn't fire on 'renting', etc.
    if any(phrase in msg_lower for phrase in _RETURNING_CALL_PHRASES):
        if _STRONG_BOOKING_RE.search(msg_lower):
            return 'jobber', 'callback context but primary intent is a new booking'
        if any(phrase in msg_lower or phrase in summary_lower for phrase in _TRANSFERRED_PHRASES):
            return 'ignore', 'returning call — caller was transferred, no follow-up needed'
        return 'email', 'caller returning a missed call'

    # Caller explicitly following up on something they initiated previously
    if _EXISTING_FOLLOWUP_RE.search(msg_lower):
        if any(phrase in msg_lower for phrase in (
            'was transferred', 'were transferred', 'transferred accordingly',
            'was connected to', 'were connected to', 'connected to an operator',
        )):
            return 'ignore', 'existing follow-up — caller was transferred'
        return 'email', 'caller following up on prior request'

    # Question about an existing reservation/booking (e.g. parking logistics, directions)
    # where no explicit follow-up verb is used — "their upcoming reservation", "my booking"
    if _EXISTING_RESERVATION_RE.search(msg_lower):
        return 'email', 'question about existing reservation — needs follow-up'

    # Confirming/checking on an existing booking — use word boundaries to avoid
    # subword false positives (e.g. 'event' inside 'seventy')
    _booking_noun_re = re.compile(r'\b(?:' + '|'.join(_BOOKING_NOUNS) + r')\b')
    if (any(verb in msg_lower for verb in _EXISTING_BOOKING_VERBS) and
            _booking_noun_re.search(msg_lower)):
        # Caller was successfully transferred — nothing left to follow up.
        # Use tight phrases only; "transferred to the" is intentionally excluded
        # because it also appears in "unable to be transferred to the ...".
        if any(phrase in msg_lower for phrase in (
            'was transferred', 'were transferred', 'transferred accordingly',
            'was connected to', 'were connected to', 'connected to an operator',
        )):
            return 'ignore', 'existing booking check — caller was transferred'
        return 'email', 'caller following up on existing booking'

    # Process/info inquiry — "how to have a truck come", "seeking information on booking"
    # Guard: if the caller also mentions a specific event type or booking keyword,
    # they're a real lead — let it fall through to the booking check below.
    if any(phrase in msg_lower for phrase in _PROCESS_INQUIRY_PHRASES):
        if not _BOOKING_RE.search(msg_lower):
            return 'email', 'process/information inquiry — not a booking'

    # Quantity/capacity/pricing/menu question — caller is researching, not booking.
    # Strong booking signal (birthday/wedding/private/etc.) still wins.
    if any(phrase in msg_lower for phrase in _INFO_QUERY_PHRASES):
        if not _STRONG_BOOKING_RE.search(msg_lower):
            return 'email', 'pricing/capacity/menu inquiry — needs answer, not a booking'

    # Caller relaying information to someone — not a new booking inquiry
    if any(phrase in msg_lower for phrase in _INFO_RELAY_PHRASES) or _LET_KNOW_RE.search(msg_lower):
        if not _STRONG_BOOKING_RE.search(msg_lower):
            return 'email', 'caller relaying information — not a booking inquiry'

    # Neighborhood/route inquiry — "will you come down Oak Street?" is not a booking
    if (any(phrase in msg_lower for phrase in _NEIGHBORHOOD_INQUIRY_PHRASES)
            or _NEIGHBORHOOD_INQUIRY_RE.search(msg_lower)):
        if not _STRONG_BOOKING_RE.search(msg_lower) or _BOOKING_DISCLAIMER_RE.search(msg_lower):
            return 'email', 'neighborhood/route inquiry — no booking intent'

    # Route-extension request — "request truck for our area" wants regular
    # service in their neighborhood, not a private booking. Needs an ops callback.
    if any(phrase in msg_lower for phrase in _ROUTE_EXTENSION_PHRASES):
        if not _STRONG_BOOKING_RE.search(msg_lower) or _BOOKING_DISCLAIMER_RE.search(msg_lower):
            return 'email', 'route-extension request — needs ops callback, not Jobber'

    # Call was handled by agent or transferred — no follow-up needed regardless of keywords.
    # Check summary too: Retell sometimes only records the transfer outcome there.
    if any(phrase in msg_lower or phrase in summary_lower for phrase in _TRANSFERRED_PHRASES):
        return 'ignore', 'call was transferred or agent-handled — no follow-up needed'

    # Truck didn't show for a booked event — urgent, needs immediate human response
    if any(phrase in msg_lower or phrase in summary_lower for phrase in _TRUCK_NO_SHOW_PHRASES):
        return 'email', 'urgent: truck no-show for active booking'

    # Caller asking if they can buy ice cream from a truck — informational, not a booking
    if _PURCHASE_INQUIRY_RE.search(msg_lower):
        return 'email', 'purchase inquiry — informational, not a booking'

    # Delivery inquiry — we don't offer delivery; needs a human to explain
    if _DELIVERY_RE.search(msg_lower) and not _STRONG_BOOKING_RE.search(msg_lower):
        return 'email', 'delivery inquiry — we do not offer delivery'

    # New booking/event inquiry — only check caller_message to avoid false positives.
    # A real booking has booking nouns/keywords (party, event, catering, birthday,
    # wedding, etc.) → Jobber. A bare truck-dispatch phrase ("requesting a truck",
    # "send a truck") with NO booking specifics is just an ask for a truck to come
    # somewhere; it needs a human to call back and gather event details, so it is an
    # email to process — NOT an auto-created Jobber request. (Per Felix 2026-06-12:
    # "requesting a truck should not lead to a jobber request; it should be an email
    # we can process." False-positive that prompted this: Jobber Request 30690176 —
    # caller said "request a truck", left no details, call dropped before contact info.)
    booking_kw = bool(_BOOKING_RE.search(msg_lower))
    truck_dispatch = bool(_TRUCK_DISPATCH_RE.search(msg_lower))
    if booking_kw or truck_dispatch:
        # Same-day events need an immediate response, not a Jobber ticket
        if _is_same_day_event(call):
            return 'slack', 'same-day event request — needs immediate response'
        if not booking_kw:
            # Truck-dispatch phrase only, no booking specifics → email for follow-up
            return 'email', 'bare truck request — no booking details; needs ops callback, not Jobber'
        return 'jobber', 'booking/service keywords detected'

    # Caller left a substantive message but no booking intent → email.
    # Note: call_successful tracks whether the agent completed its task, not whether
    # the business needs to follow up, so we do not use it to suppress routing here.
    return 'email', 'caller left a message needing follow-up'


# ── Routing ───────────────────────────────────────────────────────────────────

def _format_notes(call):
    """Plain-text summary used for email routing."""
    analysis = call.get('call_analysis') or {}
    custom   = analysis.get('custom_analysis_data') or {}
    ts       = call.get('start_timestamp', 0)
    when     = (datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                .astimezone(PACIFIC).strftime('%Y-%m-%d %I:%M %p %Z') if ts else 'unknown')

    name = custom.get('caller_first_name') or ''
    if custom.get('caller_last_name'):
        name += f' {custom["caller_last_name"]}'

    lines = [f'Call Time: {when}', f'From: {call.get("from_number", "Unknown")}']
    if name.strip():
        lines.append(f'Caller: {name.strip()}')
    if custom.get('caller_email'):
        lines.append(f'Email: {custom["caller_email"]}')
    if analysis.get('call_summary'):
        lines.append(f'Summary: {analysis["call_summary"]}')
    if custom.get('caller_message'):
        lines.append(f'Message: {custom["caller_message"]}')
    if call.get('recording_url'):
        lines.append(f'Recording: {call["recording_url"]}')
    lines.append(f'Call ID: {call.get("call_id", "")}')
    return '\n'.join(lines)


def _format_jobber_overview(call):
    """Structured overview text for the Jobber request instructions field."""
    analysis = call.get('call_analysis') or {}
    custom   = analysis.get('custom_analysis_data') or {}
    ts       = call.get('start_timestamp', 0)
    when     = (datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                .astimezone(PACIFIC).strftime('%Y-%m-%d %I:%M %p %Z') if ts else 'unknown')

    guest_count = custom.get('event_guest_count') or '—'
    event_dt    = custom.get('event_datetime')    or '—'
    event_loc   = custom.get('event_location')    or '—'
    summary     = analysis.get('call_summary')    or '—'
    message     = custom.get('caller_message')    or '—'

    lines = [
        f'How many guests: {guest_count}',
        f'Date & time:     {event_dt}',
        f'Location:        {event_loc}',
        '',
        f'Summary:',
        summary,
        '',
        f'Caller\'s request:',
        message,
        '',
        '---',
        f'Call time: {when}',
        f'Phone:     {call.get("from_number", "Unknown")}',
    ]
    if custom.get('caller_email'):
        lines.append(f'Email:     {custom["caller_email"]}')
    return '\n'.join(lines)


def _format_jobber_note(call):
    """Note content: voice chat URL at top, then full transcript."""
    public_log  = call.get('public_log_url') or ''
    recording   = call.get('recording_url')  or ''
    transcript  = call.get('transcript')     or '(no transcript available)'

    lines = []
    if public_log:
        lines.append(f'Voice chat: {public_log}')
    if recording:
        lines.append(f'Recording:  {recording}')
    lines.append('')
    lines.append('--- Transcript ---')
    lines.append(transcript)
    return '\n'.join(lines)

def _format_email_html(call, tag):
    analysis = call.get('call_analysis') or {}
    custom   = analysis.get('custom_analysis_data') or {}
    ts       = call.get('start_timestamp', 0)
    when     = (datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                .astimezone(PACIFIC).strftime('%Y-%m-%d %I:%M %p %Z') if ts else 'unknown')

    name = custom.get('caller_first_name') or ''
    if custom.get('caller_last_name'):
        name += f' {custom["caller_last_name"]}'
    name = name.strip() or '—'

    recording_url  = call.get('recording_url', '')
    recording_html = (f'<a href="{recording_url}" style="color:#d4442a">Listen to recording</a>'
                      if recording_url else '—')

    sentiment       = analysis.get('user_sentiment') or ''
    sentiment_color = '#cc0000' if 'negative' in sentiment.lower() else '#228822' if 'positive' in sentiment.lower() else '#555'

    rows = [
        ('Call Time',  when),
        ('From',       call.get('from_number', 'Unknown')),
        ('Caller',     name),
        ('Email',      custom.get('caller_email') or '—'),
        ('Sentiment',  f'<span style="color:{sentiment_color}">{sentiment or "—"}</span>'),
        ('Summary',    analysis.get('call_summary') or '—'),
        ('Message',    (custom.get('caller_message') or '—').replace('\n', '<br>')),
        ('Recording',  recording_html),
        ('Call ID',    call.get('call_id', '—')),
    ]

    rows_html = ''.join(
        f'<tr>'
        f'<td style="font-weight:600;padding:5px 16px 5px 0;vertical-align:top;'
        f'white-space:nowrap;color:#555;font-size:13px">{k}</td>'
        f'<td style="padding:5px 0;font-size:14px;color:#222">{v}</td>'
        f'</tr>'
        for k, v in rows
    )

    return f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
max-width:600px;margin:0 auto;padding:24px;color:#222">
<div style="border-left:4px solid #d4442a;padding-left:16px;margin-bottom:20px">
  <h2 style="margin:0 0 4px;color:#d4442a;font-size:18px">Mister Softee NorCal</h2>
  <p style="margin:0;color:#666;font-size:14px">{tag}</p>
</div>
<table style="border-collapse:collapse;width:100%">{rows_html}</table>
</body></html>"""

def _format_slack_same_day(call):
    """Slack message for a same-day event request."""
    analysis    = call.get('call_analysis') or {}
    custom      = analysis.get('custom_analysis_data') or {}
    name        = (custom.get('caller_first_name') or '').strip()
    phone       = call.get('from_number', 'Unknown')
    event_dt    = custom.get('event_datetime') or '—'
    event_loc   = custom.get('event_location') or '—'
    guest_count = custom.get('event_guest_count') or '—'
    message     = custom.get('caller_message') or '—'
    summary     = analysis.get('call_summary') or '—'
    recording   = call.get('recording_url') or ''
    public_log  = call.get('public_log_url') or ''

    caller_str = f'{name} ({phone})' if name else phone
    lines = [
        f':rotating_light: *Same-day event request — respond immediately*',
        f'',
        f'*Caller:* {caller_str}',
        f'*When:* {event_dt}',
        f'*Where:* {event_loc}',
        f'*Guests:* {guest_count}',
        f'*Message:* {message}',
        f'',
        f'*Summary:* {summary}',
    ]
    if recording:
        lines.append(f'*Recording:* {recording}')
    if public_log:
        lines.append(f'*Voice chat:* {public_log}')
    return '\n'.join(lines)


def route_call(call):
    action, reason = classify_call(call)
    analysis       = call.get('call_analysis') or {}
    custom         = analysis.get('custom_analysis_data') or {}
    call_id        = call.get('call_id', '')
    from_number    = call.get('from_number', '')
    first_name     = custom.get('caller_first_name') or ''
    last_name      = custom.get('caller_last_name') or ''
    caller_name    = f'{first_name} {last_name}'.strip()
    notes          = _format_notes(call)

    print(f'[Route] {call_id} from={from_number} → {action} ({reason})')

    if action == 'ignore':
        return

    if action == 'slack':
        send_slack(_format_slack_same_day(call))
        return

    if action == 'email':
        tag       = 'Voicemail' if analysis.get('in_voicemail') else 'Message'
        sentiment = (analysis.get('user_sentiment') or '').lower()
        urgent    = ('negative' in sentiment and not analysis.get('call_successful', True)) \
                    or 'urgent' in reason.lower()
        subject   = f'{"[URGENT] " if urgent else ""}[Mister Softee] {tag} from {from_number}'
        send_email(subject, notes, _format_email_html(call, tag))
        return

    if action == 'jobber':
        try:
            client_id = search_client_by_phone(from_number) if from_number else None
            if client_id:
                print(f'[Jobber] Found existing client {client_id}')
            else:
                client_id = create_client(caller_name, from_number)
                print(f'[Jobber] Created client {client_id} for {from_number}')

            display  = caller_name if caller_name else from_number
            overview = _format_jobber_overview(call)
            req      = create_jobber_request(client_id, f'Inquiry from {display}', overview)
            print(f'[Jobber] Request created: {req["title"]} (id={req["id"]})')

            note_text = _format_jobber_note(call)
            create_jobber_note(req['id'], note_text)
            print(f'[Jobber] Note added (transcript + voice chat URL)')

            req_id    = req['id']
            req_title = req['title']
            send_email(
                f'[Jobber] New request created: {req_title}',
                f'Request ID: {req_id}\nTitle: {req_title}\n\n{overview}\n\n---\n{notes}',
                f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
max-width:600px;margin:0 auto;padding:24px;color:#222">
<div style="border-left:4px solid #1a8a1a;padding-left:16px;margin-bottom:20px">
  <h2 style="margin:0 0 4px;color:#1a8a1a;font-size:18px">New Jobber Request Created</h2>
  <p style="margin:0;color:#666;font-size:13px">{req_title}</p>
</div>
<p style="font-size:13px;color:#555;margin:0 0 16px">
  <strong>Request ID:</strong> <code>{req_id}</code>
</p>
<pre style="background:#f5f5f5;padding:16px;border-radius:6px;font-size:13px;
white-space:pre-wrap;word-break:break-word">{overview}</pre>
<hr style="border:none;border-top:1px solid #eee;margin:20px 0">
<pre style="font-size:12px;color:#666;white-space:pre-wrap">{notes}</pre>
</body></html>""",
            )
        except Exception as e:
            # Do NOT archive this call as processed — re-raise so poll() queues
            # it in save_failed() for retry.  A silent email fallback would mark
            # the call as done even though the Jobber request was never created.
            print(f'[Jobber] Error — will retry (not archiving): {e}')
            raise


# ── Shadow comparison ────────────────────────────────────────────────────────
# Every real call is evaluated against the staging flow's routing logic and
# logged to shadow_log.jsonl. Staging differs from production in two ways:
#   1. "Returning a call" → A call back  (new dedicated edge)
#   2. "Already submitted a form" → Request already submitted  (new dedicated edge)
# All other calls are expected to route identically; logged for confirmation.

_RETURNING_CALL_HINTS = [
    'returning', 'called me', 'missed call', 'got a call', 'received a call',
    'someone called', 'you called', 'calling back', 'call back',
]
_ALREADY_SUBMITTED_HINTS = [
    'already submitted', 'already filled', 'submitted a form', 'submitted a request',
    'filled out', 'online form', 'web form', 'website form', 'inquiry form',
    'already sent', 'sent a request',
]

def _staging_node(call):
    analysis   = call.get('call_analysis') or {}
    custom     = analysis.get('custom_analysis_data') or {}
    transcript = (call.get('transcript') or '').lower()
    summary    = (analysis.get('call_summary') or '').lower()
    message    = (custom.get('caller_message') or '').lower()
    combined   = f'{transcript} {summary} {message}'

    if any(h in combined for h in _RETURNING_CALL_HINTS):
        return 'A call back', True

    if any(h in combined for h in _ALREADY_SUBMITTED_HINTS):
        return 'Request already submitted', True

    prod_node = (call.get('collected_dynamic_variables') or {}).get('current_node', 'unknown')
    return prod_node, False

def shadow_log(call):
    analysis    = call.get('call_analysis') or {}
    custom      = analysis.get('custom_analysis_data') or {}
    prod_node   = (call.get('collected_dynamic_variables') or {}).get('current_node', 'unknown')
    staging, is_diff = _staging_node(call)

    action, reason = classify_call(call)
    entry = {
        'call_id':        call.get('call_id'),
        'from':           call.get('from_number'),
        'ts':             call.get('start_timestamp'),
        'prod_node':      prod_node,
        'staging_node':   staging,
        'diff':           is_diff,
        'routed_to':      action,
        'route_reason':   reason,
        'sentiment':      analysis.get('user_sentiment', ''),
        'summary':        analysis.get('call_summary', ''),
        'caller_message': custom.get('caller_message', ''),
        'caller_email':   custom.get('caller_email', ''),
    }

    db = _get_db()
    if db:
        cur = db.cursor()
        cur.execute("""
            INSERT INTO shadow_log_entries
                (call_id, from_number, ts, prod_node, staging_node, diff,
                 routed_to, route_reason, sentiment, summary, caller_message, caller_email, logged_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            entry['call_id'], entry['from'], entry['ts'],
            entry['prod_node'], entry['staging_node'], entry['diff'],
            entry['routed_to'], entry['route_reason'], entry['sentiment'],
            entry['summary'], entry['caller_message'], entry['caller_email'],
            int(time.time() * 1000),
        ))
        db.commit()
    else:
        with open(SHADOW_LOG_FILE, 'a') as f:
            f.write(json.dumps(entry) + '\n')

    tag = 'DIFF' if is_diff else 'same'
    print(f'[Shadow] [{tag}] {call.get("call_id")} prod={prod_node!r} staging={staging!r}')


# ── Repeat caller detection ──────────────────────────────────────────────────

def _send_repeat_caller_alert(phone, entries):
    """Alert for a caller who has phoned 3+ times in 4 hours via Slack (or email fallback)."""
    n      = len(entries)
    header = f'{n} calls from {phone} in the last 4 hours'
    lines  = [header, '']
    for e in sorted(entries, key=lambda x: x.get('ts', 0)):
        ts     = e.get('ts', 0)
        when   = datetime.fromtimestamp(ts / 1000, tz=PACIFIC).strftime('%I:%M %p') if ts else '?'
        routed = (e.get('routed_to') or '?').upper()
        note   = (e.get('caller_message') or e.get('summary') or '—')[:120]
        lines.append(f'  {when}  [{routed}]  {note}')
    body = '\n'.join(lines)
    send_slack(f':rotating_light: *Repeat caller: {phone}*\n```{body}```')

def _check_repeat_callers(state):
    """Read shadow_log and alert if any number called 3+ times in the last 4 hours."""
    now           = int(time.time() * 1000)
    window_start  = now - REPEAT_CALLER_WINDOW_MS
    repeat_alerts = state.get('repeat_alerts', {})

    recent = []
    db = _get_db()
    if db:
        cur = db.cursor()
        cur.execute("""
            SELECT call_id, from_number, ts, routed_to, caller_message, summary
            FROM shadow_log_entries WHERE ts >= %s AND from_number IS NOT NULL
        """, (window_start,))
        for row in cur.fetchall():
            recent.append({'call_id': row[0], 'from': row[1], 'ts': row[2],
                           'routed_to': row[3], 'caller_message': row[4], 'summary': row[5]})
    elif SHADOW_LOG_FILE.exists():
        for line in SHADOW_LOG_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if e.get('ts', 0) >= window_start and e.get('from'):
                    recent.append(e)
            except json.JSONDecodeError:
                pass
    else:
        return

    by_number = {}
    for e in recent:
        by_number.setdefault(e['from'], []).append(e)

    for phone, entries in by_number.items():
        if len(entries) < REPEAT_CALLER_THRESHOLD:
            continue
        last_alert = repeat_alerts.get(phone, 0)
        if now - last_alert < REPEAT_CALLER_ALERT_TTL_MS:
            print(f'[Repeat] {phone} {len(entries)}x — already alerted')
            continue
        print(f'[Repeat] {phone} called {len(entries)}x in 4h — alerting')
        _send_repeat_caller_alert(phone, entries)
        repeat_alerts[phone] = now

    state['repeat_alerts'] = repeat_alerts


# ── Poll ──────────────────────────────────────────────────────────────────────

def _retry_failed(processed_ids):
    entries = load_failed()
    if not entries:
        return

    print(f'[Retry] {len(entries)} failed route(s) pending')
    now = time.time()
    remaining = []

    for entry in entries:
        if entry.get('next_retry_at', 0) > now:
            remaining.append(entry)
            continue

        call_id = entry['call_id']
        if call_id in processed_ids:
            continue  # already succeeded via normal poll

        attempts = entry.get('attempts', 0) + 1
        try:
            route_call(entry['call'])
            processed_ids[call_id] = int(now * 1000)
            print(f'[Retry] {call_id} succeeded on attempt {attempts}')
        except Exception as e:
            print(f'[Retry] {call_id} failed again (attempt {attempts}): {e}')
            if attempts >= 3:
                notes = _format_notes(entry['call'])
                send_email(
                    f'[Mister Softee] Routing failed after {attempts} attempts: {call_id}',
                    f'Could not route call after {attempts} attempts.\nLast error: {e}\n\n{notes}',
                )
            else:
                remaining.append({
                    'call_id':       call_id,
                    'call':          entry['call'],
                    'attempts':      attempts,
                    'last_error':    str(e),
                    'next_retry_at': now + (2 ** attempts) * 60,
                })

    save_failed(remaining)

def _in_operating_hours():
    """7 AM–10 PM PT. Used only when running in cloud (DB_URL set) so cron runs 24/7."""
    if not DB_URL:
        return True  # local cron handles the window via crontab syntax
    utc_hour = datetime.now(timezone.utc).hour
    pt_hour  = (utc_hour - 7) % 24   # approximate PDT (UTC-7); off by 1h in winter
    return 7 <= pt_hour < 22

def _send_poll_summary(counts, errors, since_str):
    """Post a Slack summary after each poll run."""
    total = sum(counts.values())
    if total == 0:
        return  # nothing new — stay silent
    lines = [f':telephone_receiver: *Poll complete* — {total} new call(s) since {since_str}']
    if counts.get('slack'):
        lines.append(f'  :rotating_light: Same-day escalations: {counts["slack"]}')
    if counts.get('jobber'):
        lines.append(f'  :spiral_note_pad: Jobber requests created: {counts["jobber"]}')
    if counts.get('email'):
        lines.append(f'  :email: Emails sent: {counts["email"]}')
    if counts.get('ignore'):
        lines.append(f'  :mute: Ignored (agent handled): {counts["ignore"]}')
    if counts.get('error'):
        lines.append(f'  :warning: Routing errors: {counts["error"]}')
        # Include the actual error messages (first 2, truncated)
        for i, err in enumerate(errors[:2]):
            cid = err.get('call_id', '')[:20]
            msg = err.get('error', '')[:120]
            lines.append(f'      `{cid}` {msg}')
    send_slack('\n'.join(lines))


def poll():
    if not _in_operating_hours():
        print('[Poll] Outside operating hours (7 AM–10 PM PT) — skipping')
        return

    state         = load_state()
    processed_ids = state.get('processed_ids', {})

    # Retry any previously failed routes before processing new calls
    _retry_failed(processed_ids)

    # Look back with a 2-minute buffer to catch calls that started just before last run
    since_ts = state.get('last_run_at', 0) - 120_000

    calls = retell_list_calls(since_ts)
    ts_str = datetime.fromtimestamp(since_ts / 1000, tz=PACIFIC).strftime('%I:%M %p')
    print(f'[Poll] {len(calls)} calls since {ts_str}')

    counts = {'jobber': 0, 'slack': 0, 'email': 0, 'ignore': 0, 'error': 0}
    errors = []
    db     = _get_db()

    for call in calls:
        call_id = call.get('call_id', '')

        if call_id in processed_ids:
            continue
        if call.get('call_status') != 'ended':
            continue

        # Atomic claim: INSERT … ON CONFLICT DO NOTHING.
        # If another run (or a manual backfill) already claimed this call_id,
        # rowcount == 0 and we skip it — prevents double-processing entirely.
        now_ms = int(time.time() * 1000)
        if db:
            cur = db.cursor()
            cur.execute(
                "INSERT INTO processed_calls (call_id, processed_at) VALUES (%s, %s) "
                "ON CONFLICT (call_id) DO NOTHING",
                (call_id, now_ms),
            )
            db.commit()
            if cur.rowcount == 0:
                print(f'[Poll] {call_id} already claimed — skipping')
                processed_ids[call_id] = now_ms
                continue
        else:
            # File-based fallback: no atomicity, but single-runner local dev is fine
            processed_ids[call_id] = now_ms

        # list-calls doesn't include call_analysis — fetch the full record
        try:
            call = retell_get_call(call_id)
        except Exception as e:
            print(f'[Poll] Could not fetch {call_id}: {e}')
            continue

        if not call.get('call_analysis'):
            print(f'[Poll] Skipping {call_id} — call_analysis not ready')
            continue

        action, _ = classify_call(call)
        try:
            route_call(call)
            counts[action] = counts.get(action, 0) + 1
        except Exception as e:
            print(f'[Poll] Routing error for {call_id} — queuing for retry: {e}')
            existing = load_failed()
            existing.append({
                'call_id':       call_id,
                'call':          call,
                'attempts':      1,
                'last_error':    str(e),
                'next_retry_at': time.time() + 60,
            })
            save_failed(existing)
            counts['error'] += 1
            errors.append({'call_id': call_id, 'error': str(e)})

        shadow_log(call)

    state['last_run_at']   = int(time.time() * 1000)
    state['processed_ids'] = processed_ids

    # Check for repeat callers across the last 4 hours
    _check_repeat_callers(state)

    save_state(state)

    _send_poll_summary(counts, errors, ts_str)


if __name__ == '__main__':
    poll()
