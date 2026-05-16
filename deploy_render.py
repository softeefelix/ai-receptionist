#!/usr/bin/env python3
"""One-time script to create the ai-receptionist-poll cron job on Render."""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ── Load credentials from existing files ──────────────────────────────────────

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

project_env   = load_env(Path(__file__).parent / '.env')
dashboard_env = load_env(Path.home() / 'softeedashboard/.env')
env = {**dashboard_env, **project_env}  # project overrides dashboard

RENDER_API_KEY = os.environ.get('RENDER_API_KEY', '')
if not RENDER_API_KEY:
    print('Set RENDER_API_KEY env var and re-run')
    sys.exit(1)

jobber_tokens_path = Path(__file__).parent / 'jobber_tokens.json'
JOBBER_TOKENS_JSON = jobber_tokens_path.read_text().strip() if jobber_tokens_path.exists() else ''

# ── Build service payload ──────────────────────────────────────────────────────

payload = {
    'autoDeploy': 'yes',
    'branch': 'main',
    'name': 'ai-receptionist-poll',
    'ownerId': 'tea-d6f8i2g8tnhs73ckap7g',
    'repo': 'https://github.com/softeefelix/ai-receptionist',
    'type': 'cron_job',
    'serviceDetails': {
        'env': 'python',
        'envSpecificDetails': {
            'buildCommand': 'pip install -r requirements.txt',
            'startCommand': 'python main.py',
        },
        'plan': 'starter',
        'region': 'oregon',
        'schedule': '*/5 * * * *',
    },
    'envVars': [
        {'key': 'RETELL_API_KEY',       'value': env.get('RETELL_API_KEY', '')},
        {'key': 'AGENTMAIL_API_KEY',    'value': env.get('AGENTMAIL_API_KEY', '')},
        {'key': 'AGENTMAIL_INBOX',      'value': env.get('AGENTMAIL_INBOX', 'mistersoftee-norcal@agentmail.to')},
        {'key': 'NOTIFY_EMAIL',         'value': env.get('NOTIFY_EMAIL', 'felix@mistersofteenorcal.com')},
        {'key': 'JOBBER_CLIENT_ID',     'value': env.get('JOBBER_CLIENT_ID', '')},
        {'key': 'JOBBER_CLIENT_SECRET', 'value': env.get('JOBBER_CLIENT_SECRET', '')},
        {'key': 'SLACK_WEBHOOK_URL',    'value': env.get('SLACK_WEBHOOK_URL', '')},
        {'key': 'DB_URL',               'value': env.get('DB_URL', '')},
        {'key': 'JOBBER_TOKENS_JSON',   'value': JOBBER_TOKENS_JSON},
    ],
}

# ── POST to Render API ─────────────────────────────────────────────────────────

data = json.dumps(payload).encode()
req = urllib.request.Request(
    'https://api.render.com/v1/services',
    data=data,
    headers={
        'Authorization': f'Bearer {RENDER_API_KEY}',
        'Content-Type': 'application/json',
    },
)
try:
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read())
    svc = result.get('service', result)
    print(f"Created:   {svc.get('name')}")
    print(f"ID:        {svc.get('id')}")
    print(f"Dashboard: {svc.get('dashboardUrl')}")
    print('Done — first deploy will start automatically.')
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f'Error {e.code}: {body}')
    sys.exit(1)
