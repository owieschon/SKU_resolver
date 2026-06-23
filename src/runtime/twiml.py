"""TwiML response builders (pure strings — no twilio SDK needed to emit).

The `/voice` endpoint uses the <Gather input="speech"> request/response flow:
Twilio does speech-to-text on its side and POSTs the transcript, we run the
gateway turn and speak the reply. This is the cheapest path to a callable
number; the AssemblyAI Media-Streams path (catalog keyterms, higher accuracy)
is the fidelity upgrade. Keeping these as pure functions makes the call flow
unit-testable without Twilio.
"""
from __future__ import annotations

from xml.sax.saxutils import escape


def gather(prompt: str, action: str, *, hints: str = '') -> str:
    """Speak `prompt`, then listen for speech and POST the result to `action`."""
    hint_attr = f' hints="{escape(hints)}"' if hints else ''
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Gather input="speech" action="{escape(action)}" method="POST" '
        f'speechTimeout="auto"{hint_attr}>'
        f'<Say>{escape(prompt)}</Say>'
        '</Gather>'
        # If the caller says nothing, re-prompt by redirecting back.
        f'<Redirect method="POST">{escape(action)}</Redirect>'
        '</Response>'
    )


def say_and_gather(text: str, action: str) -> str:
    return gather(text, action)


def say_and_hangup(text: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response><Say>{escape(text)}</Say><Hangup/></Response>')


def connect_stream(ws_url: str, *, greeting: str = '') -> str:
    """Point a call at the Media Streams WebSocket (the Streaming-STT path).
    `<Connect><Stream>` is bidirectional: Twilio streams call audio to `ws_url`
    where AssemblyAI transcribes it and the gateway runs turns. An optional
    greeting is spoken first."""
    say = f'<Say>{escape(greeting)}</Say>' if greeting else ''
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<Response>{say}<Connect>'
            f'<Stream url="{escape(ws_url)}"/>'
            '</Connect></Response>')


def dial_agent(text: str, transfer_number: str | None) -> str:
    """Escalation in voice: tell the caller, then transfer to a human if a
    transfer number is configured; otherwise take a message and hang up."""
    if transfer_number:
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                f'<Response><Say>{escape(text)}</Say>'
                f'<Dial>{escape(transfer_number)}</Dial></Response>')
    return say_and_hangup(text + ' Please hold for the next available agent, '
                                 'or call back during business hours.')
