#!/usr/bin/env python3
"""
Builds and uploads the refactored conversation flow as a staging version.
Does NOT touch the production agent.

Changes from production (v298):
  1. Master Node: 2 new edges added (before catch-all):
       - "Returning a call" → A call back node (direct message, no transfer)
       - "Already submitted a form" → Request already submitted node (direct message)
  2. Master Node: edge-1771617348155 (catering/events) updated to remove the
       "already submitted" and "returning a call" clauses (now handled by dedicated edges)
  3. Fix: dest_node_id typo in "Take a message - transfer no one picked up"
  4. Remove 3 permanently disconnected nodes:
       Conversation, Weekly Route Request, one time request
"""

import json
import os
import urllib.request
from pathlib import Path

Path(__file__).parent.joinpath('.env') and None
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

RETELL_API_KEY = os.environ['RETELL_API_KEY']
REMOVE_NODE_IDS = {
    'node-1772404895856',   # Conversation (disconnected)
    'node-1772408397370',   # Weekly Route Request (disconnected)
    'node-1772410391105',   # one time request (disconnected)
}

def build_flow():
    prod = json.loads(Path('flow_current.json').read_text())

    nodes = []
    for node in prod['nodes']:
        node_id = node.get('id', '')

        # Drop permanently disconnected nodes
        if node_id in REMOVE_NODE_IDS:
            print(f'  Removed: {node["name"]}')
            continue

        node = json.loads(json.dumps(node))  # deep copy

        # ── Master Node ───────────────────────────────────────────────────────
        if node_id == 'start-node-1771567118373':
            # Tighten the catering/events edge — remove "already submitted" and
            # "returning a call" clauses (those now have dedicated edges)
            for edge in node['edges']:
                if edge['id'] == 'edge-1771617348155-lppxnqckw':
                    edge['transition_condition']['prompt'] = (
                        "The caller is asking about catering, events, parties, or private bookings "
                        "in any way — including pricing questions, availability, general inquiries "
                        "like \"do you do catering?\", quote requests, or wanting to book/reserve "
                        "a truck for an event."
                    )
                    print('  Updated: catering/events edge (removed already-submitted / callback clauses)')

            # Insert two new edges just before the catch-all (last edge)
            catch_all = node['edges'].pop()

            node['edges'].append({
                'destination_node_id': 'node-1772641692278',
                'id': 'edge-returning-call',
                'transition_condition': {
                    'type': 'prompt',
                    'prompt': (
                        "The caller says someone from Mister Softee called them, they are returning "
                        "a missed call, or they received a call from this number and are calling back."
                    ),
                },
            })
            print('  Added: "Returning a call" edge → A call back')

            node['edges'].append({
                'destination_node_id': 'node-1772511682862',
                'id': 'edge-already-submitted',
                'transition_condition': {
                    'type': 'prompt',
                    'prompt': (
                        "The caller says they already submitted a form, request, or online inquiry "
                        "about an event, party, or booking — and they are following up on that submission."
                    ),
                },
            })
            print('  Added: "Already submitted" edge → Request already submitted')

            node['edges'].append(catch_all)

        # ── Fix dest_node_id typo ─────────────────────────────────────────────
        if node_id == 'node-1773259341860':  # Take a message - transfer no one picked up
            for edge in node['edges']:
                if 'dest_node_id' in edge and 'destination_node_id' not in edge:
                    edge['destination_node_id'] = edge.pop('dest_node_id')
                    print(f'  Fixed: dest_node_id typo in {node["name"]}')

        nodes.append(node)

    flow = {
        'start_speaker':             'agent',
        'model_choice':              prod['model_choice'],
        'model_temperature':         prod.get('model_temperature', 0),
        'global_prompt':             prod['global_prompt'],
        'knowledge_base_ids':        prod['knowledge_base_ids'],
        'tools':                     prod.get('tools', []),
        'nodes':                     nodes,
    }
    return flow


def create_flow(flow):
    data = json.dumps(flow).encode()
    req = urllib.request.Request(
        'https://api.retellai.com/create-conversation-flow',
        data=data,
        headers={
            'Authorization': f'Bearer {RETELL_API_KEY}',
            'Content-Type':  'application/json',
        },
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


def create_test_agent(flow_id):
    agent_config = {
        'agent_name':   'Conversation Flow Agent  staging',
        'voice_id':     'cartesia-Grace',
        'response_engine': {
            'type':                 'conversation-flow',
            'conversation_flow_id': flow_id,
        },
        'language':                  'en-US',
        'end_call_after_silence_ms': 21000,
        'post_call_analysis_model':  'gpt-4.1-mini',
        'analysis_summary_prompt':   (
            'Write a 1-3 sentence summary of the call based on the call transcript. '
            'Should capture the important information and actions taken during the call.'
        ),
        'analysis_successful_prompt': (
            'Evaluate whether the agent had a successful call with the user. For a successful '
            'call, the agent should have a complete conversation with user, finished the task, '
            'and have not ran into technical issues, or caused user frustration. Besides, the '
            'agent was not blocked by a call screen or encountered voicemail.'
        ),
        'analysis_user_sentiment_prompt': 'Evaluate user\'s sentiment, mood and satisfaction level.',
        'post_call_analysis_data': [
            {'type': 'system-presets', 'name': 'call_summary',
             'description': 'Write a 1-3 sentence summary of the call based on the call transcript.'},
            {'type': 'system-presets', 'name': 'call_successful',
             'description': 'Evaluate whether the agent had a successful call with the user.'},
            {'type': 'system-presets', 'name': 'user_sentiment',
             'description': 'Evaluate user\'s sentiment, mood and satisfaction level.'},
            {'name': 'caller_first_name', 'type': 'string', 'required': True,
             'description': 'Extract the caller\'s first name. Return empty string if none.'},
            {'name': 'caller_last_name', 'type': 'string', 'required': False,
             'description': 'Extract the caller\'s last name if mentioned. Return empty string if none.'},
            {'name': 'caller_email', 'type': 'string', 'required': False,
             'description': 'Extract the caller\'s email address if mentioned. Return empty string if none.'},
            {'name': 'caller_message', 'type': 'string', 'required': True,
             'description': (
                 "Summarize the caller's request or reason for calling in a clean, concise paragraph "
                 "using the caller's own words and intent. This should read like a message left for "
                 "the business — what does the caller want or need? Include any relevant details they "
                 "mentioned such as event dates, party size, location, specific items, or questions. "
                 "If the call was just a location/app inquiry with no follow-up needed, write "
                 "'Location inquiry - directed to app.' If the caller had no substantive request, "
                 "return empty string."
             )},
        ],
        'data_storage_setting': 'everything',
    }
    data = json.dumps(agent_config).encode()
    req = urllib.request.Request(
        'https://api.retellai.com/create-agent',
        data=data,
        headers={
            'Authorization': f'Bearer {RETELL_API_KEY}',
            'Content-Type':  'application/json',
        },
    )
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


if __name__ == '__main__':
    import os
    os.chdir(Path(__file__).parent)

    print('Building refactored flow...')
    flow = build_flow()
    print(f'  {len(flow["nodes"])} nodes')

    print('\nUploading to Retell...')
    result = create_flow(flow)
    flow_id = result['conversation_flow_id']
    print(f'  Flow created: {flow_id}')

    # Save for reference
    Path('flow_staging.json').write_text(json.dumps(result, indent=2))
    print('  Saved to flow_staging.json')

    print('\nCreating test agent...')
    agent = create_test_agent(flow_id)
    agent_id = agent['agent_id']
    print(f'  Agent created: {agent_id}')
    print(f'  Agent name:    {agent["agent_name"]}')

    Path('staging_agent.json').write_text(json.dumps(agent, indent=2))
    print('  Saved to staging_agent.json')

    print(f"""
Done.

  Staging flow:   {flow_id}
  Test agent:     {agent_id}
  Production:     agent_0a556e44809864d27a4f912c9a (unchanged)

To test via web call:
  https://console.retellai.com/testing?agentId={agent_id}
""")
