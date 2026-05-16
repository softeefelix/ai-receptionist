# AI Receptionist

## Overview
An AI-powered receptionist built on the Retell platform. Handles inbound calls for Mister Softee NorCal, then routes the outcome to the right system.

## Platform
- **Retell** — hosts the AI voice agent, manages call flows, generates transcripts and summaries

## Routing Logic
After each call/voicemail, classify the outcome and route accordingly:

| Outcome | Action |
|---|---|
| Service request / booking inquiry | Create request in **Jobber** |
| Message that needs follow-up | Send to **voicemail (email)** |
| No action item (spam, hangup, etc.) | Ignore |

## Connected Systems

### Retell
- Hosts the AI voice agent and call workflows
- Provides call transcripts, summaries, and conversation data
- Credentials: TBD (add `RETELL_API_KEY` to shared `.env`)

### Jobber
- Already integrated in `~/softeedashboard/server.py`
- GraphQL API, credentials in shared iCloud `.env` (`JOBBER_CLIENT_ID`, `JOBBER_CLIENT_SECRET`, `JOBBER_REFRESH_TOKEN`)
- Used here to create new requests from voicemail/call summaries

### Email (voicemail forwarding)
- TBD — likely same email/SMTP setup used elsewhere, or via Retell's built-in voicemail email

## Key Goals
1. **Monitor Retell workflow** — ensure call flows are accurate and conversations are processed correctly
2. **Process transcripts/summaries** — classify intent and route to Jobber, email, or discard

## Project Status
- [ ] Retell API access confirmed
- [ ] Routing logic designed
- [ ] Jobber request creation from transcript
- [ ] Voicemail email forwarding
- [ ] Monitoring / accuracy checks on Retell workflows
