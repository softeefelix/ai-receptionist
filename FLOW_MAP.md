# Retell Conversation Flow — Live Map & Operating Notes

Flow: `conversation_flow_857707ec18b2` (v5, live) on LIVE agent `agent_bacf3ae8d660ed30dbd2e81169` (agent v5)
Phone: (510) 513-8978 (retell-twilio, NOT version-pinned → follows live flow).
Snapshot: `flow_live_20260615_emergency_breakthrough.json` (re-pull before ANY edit).
Voice: cartesia-Grace · Model: claude-4.6-sonnet (cascading) · temp 0 · strict tools
Last audited: 2026-06-09 by Hermes. Last edit: 2026-06-15 (emergency breakthrough).

⚠ VERSION NOTE: `get-agent` reports `is_published: False` for v5 but v5 IS the live
serving version (confirmed: live calls run on agent_version=5; agent response_engine
points at flow v5). The is_published flag is cosmetically stale on this account — trust
the version the actual recent calls ran on, not the flag. `update-conversation-flow`
PATCH edits the live flow in place and takes effect immediately (no publish step needed).

⚠ Transfer destination is +1 510-xxx-6881 (Felix-confirmed 2026-06-15). NOTE: history
shows the PROVEN line was +1 408-xxx-3209 (68 successful transfers Jun 10–12, still rang
Jun 14); the 510 number was an undocumented swap with ZERO prior successful transfers.
Felix explicitly chose to KEEP 510-xxx-6881 (2026-06-15) — do not "restore" 408 without
his say-so. If emergency transfers don't ring, this number is the first suspect.

## Persona
"Lauryn" — global prompt enforces: (1) MANDATORY read-back + confirmation of all
collected info before any goodbye, (2) ONE transfer attempt only, (3) patience /
no "are you still there", (4) clarify confusing responses instead of bailing,
(5) pre-transfer triage ("what's this regarding?") so handoffs carry context.

## Node graph (16 nodes)

START: Master Node (start-node-1771567118373)  [greeting, KBs: app/payment/schedule]
  ├─ EMERGENCY / urgent "need a person NOW" ─────────────→ Transfer Call (DIRECT, any hour) [added 2026-06-15]
  ├─ truck late / no-show for booked event ──────────────→ Transfer Call (DIRECT, any hour)
  ├─ urgent OR existing-booking help ────────────────────→ Logic Split
  ├─ asks for person by name / operator ─────────────────→ Logic Split
  ├─ where's the truck right now ────────────────────────→ How to find us
  ├─ catering/events/booking/follow-up/return call ──────→ Logic Split
  ├─ missed the truck today ─────────────────────────────→ Logic Split
  ├─ one-time street visit (casual, non-event) ──────────→ Take a message - no need for transfer
  ├─ invoice / payment / billing ────────────────────────→ Take a message - no need for transfer
  ├─ anything "delivery" ────────────────────────────────→ No delivery
  ├─ confirm existing booking ───────────────────────────→ Take a message - no need for transfer
  └─ else (catch-all) ───────────────────────────────────→ Catch-All

Logic Split (branch on {{current_hour}}, built-in Retell var, America/Los_Angeles, fraction e.g. "13.5"):
  ├─ 11.5 ≤ h < 17  → Transfer Call (warm transfer → +1 510-xxx-6881, 20s ring)
  ├─ 9 ≤ h < 11.5   → Take a message morning rollout (+ EMERGENCY breakthrough → Transfer Call)
  └─ else           → Take a message off hours (+ EMERGENCY breakthrough → Transfer Call)

EMERGENCY BREAKTHROUGH (added 2026-06-15): The two no-transfer windows (morning
rollout 9–11.5, off-hours) each have an edge to Transfer Call that fires ONLY on a
genuine time-critical emergency (truck/driver accident, fire/safety incident, injury,
urgent live-event problem). Edge is ordered FIRST so it's evaluated before take-a-
message. Both nodes also carry an anti-fabrication instruction: the agent must NOT
say it's connecting/transferring and must NEVER claim "no one picked up" outside a
real emergency — it just takes a message. Origin: 10:37 AM 6/15 call
(call_4fef6ebb068e72d6815bea4a014) where the agent faked "no one was able to pick up"
to an insistent caller at 10:37 (morning-rollout window) — no transfer was ever placed
(empty tool_calls; node path Master→Logic Split→morning rollout, never touched Transfer).

Transfer Call:
  └─ on failure → Take a message - transfer no one picked up
       └─ caller insists / urgent → BACK to Transfer Call (⚠ contradicts global one-attempt rule)

How to find us [global node — triggers from anywhere on "where's the truck"]:
  - pushes the MisterSofteeNorCal app first; else calls get_nearest_truck tool
  - tool: POST https://mistersoftee-truck-api.onrender.com/nearest-truck {location} (15s timeout)
  - edges: more questions → Master Node · satisfied → End Call · leave message → Logic Split

All "Take a message" variants (off hours / morning rollout / no-transfer / transfer-failed):
  - collect name + email-or-phone (+ event date/headcount/location if catering, gently)
  - mandatory read-back incl. reason → confirm → End Call

End Call: "Thanks so much for calling Mister Softee! Have a great day."

## ORPHANED nodes (exist in flow, NO inbound edges — currently dead code)
  - Conversation (node-1772404895856): "weekly or one-time?" fork
  - Weekly Route Request (node-1772408397370): route-request script (name/address/headcount)
  - one time request (node-1772410391105): "no one-time stops, want an event instead?"
  - Request already submitted (node-1772511682862)
  - A call back (node-1772641692278)
  The last two were the STAGING experiment (main.py shadow_log simulates routing to
  them); nodes were added but edges never wired during promotion. The weekly-route
  trio was stranded when Master began routing street-visit requests straight to
  Take a message.

## Knowledge bases (account has 5)
  Flow-level: How to find us - our app · Payment Methods · Menu
  Master-node-level: How to find us - our app · Payment Methods · Schedule and locations
  ⚠ "Booking Events" KB (knowledge_base_be2a82f9b81f5f90) attached NOWHERE.

## Agent-level settings that bite
  - max_call_duration_ms = 600000 (10 min) — FIXED in agent v3 (2026-06-09, Hermes).
    Was 295000 (4m55s), which hard-cut live calls; see release note on v3.
  - end_call_after_silence_ms = 21000
  - interruption_sensitivity 0.92, responsiveness 0.77, backchannel on
  - post-call analysis: gpt-4.1-mini, custom summary prompt
  - webhook_url: none (poller architecture is intentional — see main.py)

## Editing rules (hard-won)
  1. ONLY edit agent_bacf3ae8d660ed30dbd2e81169 / flow 857707ec18b2. Everything
     else is labeled draft/old-prod "do not edit".
  2. Pull fresh flow JSON before editing; PATCH via
     /update-conversation-flow/conversation_flow_857707ec18b2 (partial updates OK).
  2b. AGENT-level edits: published versions are LOCKED ("Cannot update published
      agent other than version title"). Correct sequence:
        POST /create-agent-version/{agent} {"base_version": N}   -> new draft vN+1
        PATCH /update-agent/{agent}?version=N+1 {changes + version_description}
        POST /publish-agent-version/{agent} {"version": N+1, "version_description": ...}
      Prod number (510) 513-8978 is NOT version-pinned — it follows latest
      published, so publish == live instantly. Put full release notes in
      version_description (what + why + scope) per Felix's release standard.
  3. After flow edits, re-publish the agent version if required and verify with a
     test call before walking away.
  4. The poller (main.py classify_call) keys off node names via
     collected_dynamic_variables.current_node — RENAMING A NODE can break routing
     classification. Grep main.py for the node name before renaming anything.
  5. flow_current.json / flow_staging.json in repo describe the OLD flow
     (conversation_flow_1333df17b2df, old agent ccf0da...). Do not promote from them.
  6. Phone +151****7345 (Telnyx test) still points at old-prod agent 0a556e... — fine,
     no traffic, leave it.

## Downstream (main.py poller — GitHub Actions, every 5 min 7a–10p PT)
  classify_call() → jobber request / email via AgentMail / ignore
  Same-day-event detector → urgent email + Slack. Repeat-caller (3+ in 4h) → Slack.
  Jobber tokens shared with softeedashboard via softy_dashboard_app_kv (race-safe).
