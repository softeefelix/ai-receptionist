#!/usr/bin/env python3
"""
Promote the staging conversation flow to production.

Patches the production agent's response_engine to use the staging flow.
⚠  This affects ALL live calls immediately. Only run when satisfied with
   shadow log results. Production is not touched until you type "promote".

Production agent:  agent_0a556e44809864d27a4f912c9a
Current prod flow: conversation_flow_1333df17b2df  (v298)
Staging flow:      conversation_flow_55c68a8438eb
"""

import json
import os
import urllib.request
from pathlib import Path


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

RETELL_API_KEY  = os.environ['RETELL_API_KEY']
PROD_AGENT_ID   = 'agent_0a556e44809864d27a4f912c9a'
PROD_FLOW_ID    = 'conversation_flow_1333df17b2df'
STAGING_FLOW_ID = 'conversation_flow_55c68a8438eb'


def get_agent(agent_id):
    req = urllib.request.Request(
        f'https://api.retellai.com/get-agent/{agent_id}',
        headers={'Authorization': f'Bearer {RETELL_API_KEY}'},
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


def patch_agent_flow(agent_id, flow_id):
    data = json.dumps({
        'response_engine': {
            'type': 'conversation-flow',
            'conversation_flow_id': flow_id,
        },
    }).encode()
    req = urllib.request.Request(
        f'https://api.retellai.com/update-agent/{agent_id}',
        data=data,
        method='PATCH',
        headers={
            'Authorization': f'Bearer {RETELL_API_KEY}',
            'Content-Type':  'application/json',
        },
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


if __name__ == '__main__':
    print()
    print('=' * 62)
    print('  PROMOTE STAGING FLOW TO PRODUCTION')
    print('=' * 62)
    print(f'  Production agent:   {PROD_AGENT_ID}')
    print(f'  Replacing flow:     {PROD_FLOW_ID}')
    print(f'  With staging flow:  {STAGING_FLOW_ID}')
    print()

    # Verify current state before touching anything
    print('  Verifying current agent state...')
    agent = get_agent(PROD_AGENT_ID)
    current_flow = agent.get('response_engine', {}).get('conversation_flow_id', 'unknown')

    if current_flow == STAGING_FLOW_ID:
        print('  Already on staging flow — nothing to do.')
        raise SystemExit(0)

    if current_flow != PROD_FLOW_ID:
        print(f'  WARNING: Current flow is {current_flow!r}')
        print(f'           Expected         {PROD_FLOW_ID!r}')
        print()
        print('  Agent is not on the expected production flow.')
        print('  Review manually before promoting.')
        raise SystemExit(1)

    print(f'  Confirmed: agent is on {current_flow}')
    print()
    print('  This will go live immediately for all incoming calls.')
    print()

    confirm = input('  Type "promote" to continue, anything else to cancel: ').strip()
    if confirm != 'promote':
        print('  Cancelled.')
        raise SystemExit(0)

    print()
    print('  Patching production agent...')
    result   = patch_agent_flow(PROD_AGENT_ID, STAGING_FLOW_ID)
    new_flow = result.get('response_engine', {}).get('conversation_flow_id', 'unknown')

    if new_flow == STAGING_FLOW_ID:
        print(f'  Done. Production agent is now using: {new_flow}')
        print()
        print('  To roll back, run:')
        print(f'    python3 promote_staging.py  (after swapping PROD/STAGING flow IDs)')
    else:
        print(f'  Unexpected response — verify in Retell console.')
        print(json.dumps(result, indent=2))
        raise SystemExit(1)
    print()
