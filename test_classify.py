#!/usr/bin/env python3
"""Regression tests for context-first call classification.

Trigger words (party / event / corporate / booking) must not win when the
caller's actual purpose is something else (receipt, follow-up, vendor pitch,
truck tracking, already-booked logistics).
"""

import os

# main.py requires these at import time
for k in (
    'RETELL_API_KEY', 'AGENTMAIL_API_KEY',
    'JOBBER_CLIENT_ID', 'JOBBER_CLIENT_SECRET',
):
    os.environ.setdefault(k, 'test')

from main import classify_call  # noqa: E402


def _call(message, summary='', *, voicemail=False, disconnect=None, email='',
          current_node=None):
    payload = {
        'call_id': 'test',
        'from_number': '+1' + '555' + '555' + '0100',
        'disconnection_reason': disconnect,
        'call_analysis': {
            'in_voicemail': voicemail,
            'call_summary': summary,
            'custom_analysis_data': {
                'caller_message': message,
                'caller_email': email,
            },
        },
    }
    if current_node is not None:
        payload['collected_dynamic_variables'] = {'current_node': current_node}
    return payload


def _action(message, summary='', **kw):
    return classify_call(_call(message, summary, **kw))[0]


def _reason(message, summary='', **kw):
    return classify_call(_call(message, summary, **kw))[1]


# ── Context beats trigger words ──────────────────────────────────────────────

def test_receipt_for_past_party_is_email_not_jobber():
    action, reason = classify_call(_call(
        'Steve Montebago is requesting a receipt for the party they just had '
        'and provided his contact details for follow-up.',
        'The caller requested a receipt for a recent party.',
    ))
    assert action == 'email', (action, reason)
    assert 'receipt' in reason.lower() or 'existing' in reason.lower() or 'context' in reason.lower()


def test_quote_followup_with_event_word_is_email_not_jobber():
    action, reason = classify_call(_call(
        'Nicole Feldman is following up on a quote for an event this Friday '
        'and wants to know if the quote includes tips.',
        'The caller called to follow up on a quote for an event this Friday.',
    ))
    assert action == 'email', (action, reason)


def test_vendor_corporate_app_pitch_is_email_not_jobber():
    action, reason = classify_call(_call(
        'Sarosh Sopariwala is reaching out with a corporate inquiry to offer '
        'rebuilding the mobile app completely free and wants the team to follow up.',
        'The caller contacted with a corporate inquiry about rebuilding their mobile app for free.',
    ))
    assert action == 'email', (action, reason)
    assert action != 'jobber'


def test_parking_for_already_booked_event_is_email_not_jobber():
    action, reason = classify_call(_call(
        'Cindy Maria is requesting a callback to discuss the parking location '
        'for trucks at the Saint John Vianney Festival, an event she has already booked.',
        'The caller inquired about the parking location for trucks at an event she has already booked.',
    ))
    assert action == 'email', (action, reason)


def test_refund_on_existing_party_booking_is_email_not_jobber():
    action, reason = classify_call(_call(
        'Steve Monnebago is calling to address a problem with a party booking '
        'date in his homeowners association and to inquire about a refund.',
        'The caller called to address an issue regarding a party booking and a refund.',
    ))
    assert action == 'email', (action, reason)


def test_reschedule_existing_reservation_is_email_not_jobber():
    action, reason = classify_call(_call(
        'Danny from Vanta is calling to reschedule a reservation for tomorrow, '
        'August 11th, in San Francisco.',
        'The caller called to reschedule a reservation for August 11th in San Francisco.',
    ))
    assert action == 'email', (action, reason)


def test_track_truck_for_private_event_is_not_new_jobber():
    action, reason = classify_call(_call(
        'The caller wants to track the Mister Softee truck coming to their private event '
        'and is seeking assistance with tracking options.',
        'The user called to inquire about tracking a truck for a private event. '
        'The agent informed the user that the best way to track the truck is through the app.',
    ))
    assert action != 'jobber', (action, reason)


def test_location_now_in_city_is_ignore_even_if_message_mentions_truck():
    action, reason = classify_call(_call(
        'The caller wanted to know if there was a Mister Softee truck currently in Newark, California, '
        'and requested the agent to check the location for them.',
        'The caller inquired about the current location of a Mister Softee truck in Newark. '
        'The agent recommended using the MisterSofteeNorCal app.',
    ))
    assert action == 'ignore', (action, reason)


def test_send_truck_to_address_is_not_location_ignore():
    # "wants to know if a truck can be sent" is a dispatch ask, not a location lookup.
    action, reason = classify_call(_call(
        'The caller wants to know if a Mister Softee truck can be sent to Fremont, California, '
        'specifically San Pedro Drive, and intended to provide a phone number for follow-up.',
        'The caller requested a truck be sent to San Pedro Drive in Fremont.',
    ))
    assert action != 'ignore', (action, reason)


def test_bring_truck_to_market_event_is_not_location_ignore():
    action, reason = classify_call(_call(
        'The caller wants to know if the ice cream truck can be brought to a market event '
        'at the Alameda County fairs because it is very hot, and is seeking assistance.',
        'The caller asked if a truck can be brought to a market event.',
    ))
    assert action != 'ignore', (action, reason)


def test_stop_by_office_today_is_not_location_ignore():
    action, reason = classify_call(_call(
        'The caller wants to know if there is a Mister Softee truck in their area today '
        'that could stop by their office in Foster City and prefers to speak with someone.',
        'The caller asked if a truck in the area could stop by their office today.',
    ))
    assert action != 'ignore', (action, reason)


def test_locate_truck_and_private_event_access_is_not_jobber():
    """Exact 33065536 / call_32b878a4d3fa02a34b8096e8730 — locate via app +
    'can I visit a truck during a private event'. Not a rental/booking."""
    action, reason = classify_call(_call(
        'The caller wanted to locate a Mister Softee truck using the app and asked '
        'if they could visit a truck during a private event, seeking clarification '
        'on access during such events.',
        'The caller inquired about locating a Mister Softee truck and was advised '
        'to use the MisterSofteeNorCal app for real-time locations. The caller also '
        'asked if they could visit a truck during a private event, but the agent '
        'did not have that information and offered to have someone from the team '
        'follow up.',
        current_node='Catch-All',
    ))
    assert action != 'jobber', (action, reason)
    assert action == 'email', (action, reason)


# ── Real new bookings still go to Jobber ─────────────────────────────────────

def test_new_birthday_booking_with_details_is_jobber():
    action, reason = classify_call(_call(
        'Jefferson Simmons is looking to book a Mister Softee truck for a party '
        'at his house in Pacifica on August 30th starting around 6 PM for about '
        'half an hour. He expects about 100 guests.',
        'The caller contacted to arrange a party booking for August 30th in Pacifica.',
        email='jeff@example.com',
    ))
    assert action == 'jobber', (action, reason)


def test_new_catering_booking_with_details_is_jobber():
    action, reason = classify_call(_call(
        'The caller wants to book catering for an event on August 25th at 4 PM '
        'for roughly fifty people in South San Francisco and provided their phone number.',
        'The caller contacted to book catering for an event on August 25th.',
        email='x@example.com',
    ))
    assert action == 'jobber', (action, reason)


# ── Hard rails that must not regress ─────────────────────────────────────────

def test_successful_transfer_is_ignore():
    action, reason = classify_call(_call(
        'The caller wants to book a birthday party.',
        'The caller contacted to book a birthday party. The agent transferred the call.',
        disconnect='call_transfer',
    ))
    assert action == 'ignore', (action, reason)


def test_successful_transfer_stays_ignore_even_if_agent_took_a_message():
    """Transfer ignore must outrank take-a-message (don't email completed transfers)."""
    action, reason = classify_call(_call(
        'The caller wants to book a birthday party. The agent took a message.',
        'The agent took a message and then transferred the caller.',
        disconnect='call_transfer',
        current_node='Transfer Call',
        email='x@example.com',
    ))
    assert action == 'ignore', (action, reason)


def test_returning_a_call_is_email():
    action, reason = classify_call(_call(
        'The caller is returning a missed call from Mister Softee.',
        'The caller is returning a phone call.',
    ))
    assert action == 'email', (action, reason)


def test_known_staff_name_is_email_not_jobber():
    action, reason = classify_call(_call(
        'Lulu from Protein Research called to leave a message for Chelsea about '
        'an event on August 29th at Emerald Glen Park in Dublin.',
        'The caller called to speak with Chelsea regarding an event.',
    ))
    assert action == 'email', (action, reason)


# ── Voicemail / take-a-message never creates a Jobber request ────────────────
# Felix 2026-08-20: Jobber request 33067593 / call_e6a4337c0c0a1ee3611e884ffc1
# (Elise-Marie Brown +17604206196). Caller asked for STATUS of an existing
# truck rental. Agent took a message (in_voicemail=false — Retell almost
# never sets that flag). Keyword "rental" still created a Jobber request.
# Law: a Jobber request is ONLY for new bookings.

def test_status_of_existing_truck_rental_is_email_not_jobber():
    """Exact 33067593 payload — status of existing rental, agent took a message."""
    action, reason = classify_call(_call(
        'The caller wants to know the status of their truck rental and is '
        'willing to provide contact information for a follow-up from the team.',
        'The caller inquired about the status of their truck rental. The agent '
        'informed the caller that they could not access rental booking details '
        'but offered to take the caller\'s information for a follow-up by the '
        'appropriate team.',
        current_node='Catch-All',
    ))
    assert action != 'jobber', (action, reason)
    assert action == 'email', (action, reason)


def test_in_voicemail_flag_never_creates_jobber_even_with_booking_words():
    action, reason = classify_call(_call(
        'The caller wants to book a birthday party on September 12th for 80 guests.',
        'The caller left a voicemail about booking a birthday party.',
        voicemail=True,
        email='x@example.com',
    ))
    assert action == 'email', (action, reason)
    assert 'voicemail' in reason.lower()


def test_take_a_message_node_never_creates_jobber():
    action, reason = classify_call(_call(
        'The caller wants to book a truck for a birthday party next Saturday.',
        'The office team was unavailable so the agent took a message for follow-up.',
        current_node='Take a message off hours',
        email='x@example.com',
    ))
    assert action != 'jobber', (action, reason)
    assert action == 'email', (action, reason)


def test_agent_took_a_message_summary_never_creates_jobber():
    action, reason = classify_call(_call(
        'The caller wants to book catering for a school event.',
        'The agent took a message with the caller\'s phone number and noted '
        'that someone would follow up for more details.',
        current_node='End Call',
        email='x@example.com',
    ))
    assert action != 'jobber', (action, reason)
    assert action == 'email', (action, reason)


def test_unclaim_call_drops_from_processed_ids():
    from main import _unclaim_call
    ids = {'call_abc': 1, 'call_keep': 2}
    _unclaim_call('call_abc', ids)
    assert 'call_abc' not in ids
    assert ids['call_keep'] == 2


if __name__ == '__main__':
    import traceback
    tests = [v for k, v in globals().items() if k.startswith('test_')]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f'  ok  {fn.__name__}')
        except Exception as e:
            failed += 1
            print(f' FAIL {fn.__name__}: {e}')
            traceback.print_exc()
    print(f'\n{len(tests) - failed}/{len(tests)} passed')
    raise SystemExit(1 if failed else 0)
