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

def _jobber_load_tokens():
    db = _get_db()
    if db:
        cur = db.cursor()
        cur.execute("SELECT tokens_json FROM jobber_tokens WHERE id = 1")
        row = cur.fetchone()
        if row:
            return json.loads(row[0])
        # DB has no tokens yet — fall through to seed from env or file

    if JOBBER_TOKENS_FILE.exists():
        return json.loads(JOBBER_TOKENS_FILE.read_text())
    env_json = os.environ.get('JOBBER_TOKENS_JSON')
    if env_json:
        return json.loads(env_json)
    return None

def _jobber_save_tokens(tokens):
    db = _get_db()
    if db:
        cur = db.cursor()
        cur.execute("""
            INSERT INTO jobber_tokens (id, tokens_json, updated_at) VALUES (1, %s, %s)
            ON CONFLICT (id) DO UPDATE SET tokens_json = EXCLUDED.tokens_json, updated_at = EXCLUDED.updated_at
        """, (json.dumps(tokens), int(time.time() * 1000)))
        db.commit()
    else:
        JOBBER_TOKENS_FILE.write_text(json.dumps(tokens, indent=2))

def get_jobber_token(force_refresh=False):
    tokens = _jobber_load_tokens()
    if not tokens:
        raise RuntimeError('No Jobber tokens — copy jobber_tokens.json from softeedashboard')

    expires_at = tokens.get('expires_at', 0)
    if not force_refresh and expires_at and time.time() < expires_at - 300:
        return tokens['access_token']

    data = urllib.parse.urlencode({
        'client_id':     JOBBER_CLIENT_ID,
        'client_secret': JOBBER_CLIENT_SECRET,
        'grant_type':    'refresh_token',
        'refresh_token': tokens['refresh_token'],
    }).encode()
    req = urllib.request.Request(
        JOBBER_TOKEN_URL, data=data,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    resp = urllib.request.urlopen(req)
    new_tokens = json.loads(resp.read())
    new_tokens['saved_at']   = time.time()
    new_tokens['expires_at'] = time.time() + new_tokens.get('expires_in', 3600) - 60
    _jobber_save_tokens(new_tokens)
    return new_tokens['access_token']

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
    'reserve', 'reservation', 'schedule',
    'event', 'parties', 'function',
]

# Subset of _BOOKING_KEYWORDS that unambiguously signal a NEW booking, even in callback context.
# 'event', 'schedule', 'reservation' are intentionally excluded — they often refer to existing ones.
_STRONG_BOOKING_KEYWORDS = {
    'book', 'booking',
    'catering', 'cater', 'hire', 'rent', 'rental', 'private',
    'birthday', 'wedding', 'corporate', 'celebration',
    'reserve', 'parties', 'function',
}

# Caller is returning a missed call — route to email unless strong booking intent is clear
_RETURNING_CALL_PHRASES = [
    'returning a call', 'returning your call', 'returning the call',
    'calling back', 'call back', 'missed call', 'received a call',
    'got a call', 'called me', 'you called', 'someone called',
]

# Caller is checking on an existing booking — email, not a new Jobber request
_EXISTING_BOOKING_VERBS = [
    'confirm', 'confirming', 'check on', 'checking on',
    'follow up', 'following up', 'verify', 'verifying',
]
_BOOKING_NOUNS = ['reservation', 'booking', 'appointment', 'event']

# Caller explicitly left no actionable message — safe to ignore
_NO_MESSAGE_INDICATORS = [
    'did not leave', 'no specific message', 'did not provide',
    'declined to provide', 'no message left', 'did not specify',
    'did not have a clear request', 'no coherent request',
]

# Caller is specifically trying to reach a named person — email even if no message left
_TRYING_TO_REACH_PHRASES = [
    'trying to reach',
    'trying to get in touch',
    'called to reach',
    'calling to reach',
]

# Retell agent writes this exact string when the call was a location/app lookup
_LOCATION_INQUIRY_MARKER = 'location inquiry - directed to app'

# Agent transferred the caller or fully resolved their inquiry — no follow-up needed
_TRANSFERRED_PHRASES = [
    'was transferred',
    'transferred accordingly',
    'transferred to the',
    'transferred to an',
    'was connected to',
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

def classify_call(call):
    """Returns ('jobber' | 'email' | 'ignore', reason).

    Classification improves over time via shadow_annotations.json — run
    `python3 review_shadow.py --annotate` after calls accumulate.
    """
    analysis = call.get('call_analysis') or {}
    custom   = analysis.get('custom_analysis_data') or {}

    in_voicemail   = analysis.get('in_voicemail', False)
    caller_message = (custom.get('caller_message') or '').strip()
    caller_email   = (custom.get('caller_email') or '').strip()
    msg_lower      = caller_message.lower()

    if in_voicemail:
        return 'email', 'voicemail'

    # Agent explicitly flagged this as a location/app lookup, or caller resolved it via app
    if _LOCATION_INQUIRY_MARKER in msg_lower:
        return 'ignore', 'location/app inquiry — agent handled'
    if any(phrase in msg_lower for phrase in _APP_RESOLUTION_PHRASES):
        return 'ignore', 'caller resolved inquiry via app — no follow-up needed'

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
        return 'ignore', 'caller left no specific message'

    # Returning a missed call — email, UNLESS strong new-booking intent is present
    if any(phrase in msg_lower for phrase in _RETURNING_CALL_PHRASES):
        if any(kw in msg_lower for kw in _STRONG_BOOKING_KEYWORDS):
            return 'jobber', 'callback context but primary intent is a new booking'
        return 'email', 'caller returning a missed call'

    # Confirming/checking on an existing booking — use word boundaries to avoid
    # subword false positives (e.g. 'event' inside 'seventy')
    _booking_noun_re = re.compile(r'\b(?:' + '|'.join(_BOOKING_NOUNS) + r')\b')
    if (any(verb in msg_lower for verb in _EXISTING_BOOKING_VERBS) and
            _booking_noun_re.search(msg_lower)):
        # Caller was successfully transferred — nothing left to follow up
        if 'transferred' in msg_lower or 'was connected' in msg_lower:
            return 'ignore', 'existing booking check — caller was transferred'
        return 'email', 'caller following up on existing booking'

    # New booking/event inquiry — only check caller_message to avoid false positives
    if any(kw in msg_lower for kw in _BOOKING_KEYWORDS):
        return 'jobber', 'booking/service keywords detected'

    # Call was handled by agent or transferred — no follow-up needed
    if any(phrase in msg_lower for phrase in _TRANSFERRED_PHRASES):
        return 'ignore', 'call was transferred or agent-handled — no follow-up needed'

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
                .astimezone().strftime('%Y-%m-%d %I:%M %p %Z') if ts else 'unknown')

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
                .astimezone().strftime('%Y-%m-%d %I:%M %p %Z') if ts else 'unknown')

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
                .astimezone().strftime('%Y-%m-%d %I:%M %p %Z') if ts else 'unknown')

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

    if action == 'email':
        tag       = 'Voicemail' if analysis.get('in_voicemail') else 'Message'
        sentiment = (analysis.get('user_sentiment') or '').lower()
        urgent    = 'negative' in sentiment and not analysis.get('call_successful', True)
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
            print(f'[Jobber] Error — falling back to email: {e}')
            send_email(f'[Mister Softee] Jobber error for {from_number}',
                       f'Failed to create Jobber request:\n{e}\n\n{notes}')


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
        when   = datetime.fromtimestamp(ts / 1000).strftime('%I:%M %p') if ts else '?'
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

def _send_poll_summary(counts, since_str):
    """Post a Slack summary after each poll run."""
    total = sum(counts.values())
    if total == 0:
        return  # nothing new — stay silent
    lines = [f':telephone_receiver: *Poll complete* — {total} new call(s) since {since_str}']
    if counts.get('jobber'):
        lines.append(f'  :spiral_note_pad: Jobber requests created: {counts["jobber"]}')
    if counts.get('email'):
        lines.append(f'  :email: Emails sent: {counts["email"]}')
    if counts.get('ignore'):
        lines.append(f'  :mute: Ignored (agent handled): {counts["ignore"]}')
    if counts.get('error'):
        lines.append(f'  :warning: Routing errors: {counts["error"]}')
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
    ts_str = datetime.fromtimestamp(since_ts / 1000).strftime('%I:%M %p')
    print(f'[Poll] {len(calls)} calls since {ts_str}')

    counts = {'jobber': 0, 'email': 0, 'ignore': 0, 'error': 0}

    for call in calls:
        call_id = call.get('call_id', '')

        if call_id in processed_ids:
            continue
        if call.get('call_status') != 'ended':
            continue

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
            processed_ids[call_id] = int(time.time() * 1000)
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
            processed_ids[call_id] = int(time.time() * 1000)
            counts['error'] += 1

        shadow_log(call)

    state['last_run_at']   = int(time.time() * 1000)
    state['processed_ids'] = processed_ids

    # Check for repeat callers across the last 4 hours
    _check_repeat_callers(state)

    save_state(state)

    _send_poll_summary(counts, ts_str)


if __name__ == '__main__':
    poll()
