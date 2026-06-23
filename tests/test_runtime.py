"""P4 runtime — the deployed surface end-to-end over HTTP via TestClient
(in-process, no network). Proves the golden conversation works through the
real ASGI app and that the Twilio /voice flow returns valid TwiML with the
gates intact.
"""
from __future__ import annotations

import pytest

pytest.importorskip('fastapi')
from fastapi.testclient import TestClient  # noqa: E402

from runtime.app import create_app  # noqa: E402


@pytest.fixture(scope='module')
def client():
    return TestClient(create_app())


# -- chat API: full golden conversation over HTTP ----------------------------

def test_chat_golden_conversation_over_http(client):
    s = client.post('/v1/sessions', params={'channel_id': 'web1'}).json()
    sid, tok = s['session_id'], s['token']

    def turn(text):
        return client.post('/v1/turns', json={
            'session_id': sid, 'token': tok, 'text': text,
            'channel': 'typed'}).json()

    # availability ungated
    a = turn('is K5-24SBC in stock?')
    assert a['kind'] == 'availability' and 'availability' in a

    # pricing refused before verification
    p = turn('how much is K5-24SBC?')
    assert p['kind'] == 'pricing' and p.get('refused') == 'pricing_unauthorized'

    # verify, then price
    v = turn('my account number is 1001')
    assert v['session_state'] == 'verified'
    p2 = turn('how much is K5-24SBC?')
    assert p2.get('price') and p2['price']['source'] == 'verified_account_self'


def test_agent_tool_reuses_gates_with_verbatim_say(client):
    # The voice-agent tool: one endpoint, session keyed by caller_id. It must
    # return a `say` string AND enforce the gates (never-invent, pricing behind
    # verification) — because the agent can only get parts/prices through it.
    def tool(text, caller='CALL-1'):
        return client.post('/agent/turn',
                           json={'caller_id': caller, 'text': text}).json()

    a = tool('is K5-24SBC in stock?')
    assert a['kind'] == 'availability' and a['say']            # speakable answer

    p = tool('how much is K5-24SBC?')
    assert p['refused'] == 'pricing_unauthorized'              # gate held in the tool
    assert '$' not in p['say']                                 # no price leaked

    v = tool('my account number is 1001')
    assert v['session_state'] == 'verified'                    # verification persists
    p2 = tool('how much is K5-24SBC?')
    assert p2.get('refused') is None and p2['say']             # now priced
    assert p2['kind'] == 'pricing'


def test_agent_tool_requires_secret_when_configured(client, monkeypatch):
    # Containment: with AGENT_TOOL_SECRET set, the tool rejects callers without a
    # matching X-Agent-Token (closes the open-tunnel hole); fails open in dev.
    monkeypatch.setenv('AGENT_TOOL_SECRET', 's3cr3t')
    body = {'caller_id': 'CALL-AUTH', 'text': 'is K5-24SBC in stock?'}
    assert client.post('/agent/turn', json=body).status_code == 403          # missing
    assert client.post('/agent/turn', json=body,
                       headers={'X-Agent-Token': 'wrong'}).status_code == 403  # mismatch
    ok = client.post('/agent/turn', json=body, headers={'X-Agent-Token': 's3cr3t'})
    assert ok.status_code == 200 and ok.json()['say']                         # correct


def test_agent_tool_captures_same_improvement_data():
    # Parity: the hosted-agent path feeds the continuous-improvement loop just
    # like the agent's own voice calls do.
    from gateway_fixtures import _shared_catalog

    from gateway import ContinuousImprovement, CorrectionStore, ShadowObserver
    from resolution import ResolutionService
    from runtime.app import create_app
    from sku_translator import InMemoryStore
    cat, ver = _shared_catalog()
    corr = CorrectionStore(cat)
    svc = ResolutionService(cat, InMemoryStore(), catalog_version=ver,
                            learned_aliases=corr)
    ci = ContinuousImprovement(
        ShadowObserver(svc, catalog=cat, corrections=corr), corr, review_every=99)
    c = TestClient(create_app(improvement=ci))
    c.post('/agent/turn',
           json={'caller_id': 'C9', 'text': 'do you stock the qq9zz adapter'})
    assert ci.pending_review().opportunities      # captured, same as voice path


def test_tools_manifest_served(client):
    m = client.get('/v1/tools.json').json()
    assert m['name'] == 'sku_service_turn'


def test_openapi_contract_served(client):
    spec = client.get('/openapi.json').json()
    assert '/v1/turns' in spec['paths']


def test_healthz(client):
    assert client.get('/healthz').json()['ok'] is True


# -- Twilio voice flow (TwiML) -----------------------------------------------

def _voice(client, call_sid, speech=None):
    data = {'CallSid': call_sid}
    if speech is not None:
        data['SpeechResult'] = speech
    return client.post('/voice', data=data).text


def test_voice_greeting_then_gather(client):
    xml = _voice(client, 'CA-test-1')
    assert xml.startswith('<?xml')
    assert '<Gather' in xml and 'How can I help you' in xml


def test_voice_availability_turn_speaks_answer(client):
    _voice(client, 'CA-test-2')                       # greeting (opens session)
    # Voice requires a readback before identifying (the #11 guarantee).
    readback = _voice(client, 'CA-test-2', speech='K5-24SBC')
    # On the voice path the SKU is spelled for the ear ("K 5, 24 S B C"), not the
    # literal "K5-24SBC" a chat client would get.
    assert 'K 5, 24 S B C' in readback and '<Gather' in readback
    # Caller affirms a discriminating attribute -> availability answer.
    answer = _voice(client, 'CA-test-2', speech='yes the chrome one')
    assert 'stock' in answer.lower() and '<Gather' in answer


def test_voice_pricing_unverified_refused(client):
    _voice(client, 'CA-test-3')
    _voice(client, 'CA-test-3', speech='K5-24SBC')         # readback
    _voice(client, 'CA-test-3', speech='yes the chrome one')  # confirmed
    xml = _voice(client, 'CA-test-3', speech='how much for that one')
    assert 'verify' in xml.lower()                     # refusal + offer, no price
    assert '$' not in xml                              # no price leaked over voice


def test_voice_out_of_scope_escalates_to_dial_or_message(client):
    _voice(client, 'CA-test-4')
    xml = _voice(client, 'CA-test-4', speech='where is my order')
    # escalation path: either a Dial (if transfer configured) or a message+hangup
    assert '<Dial>' in xml or '<Hangup/>' in xml


def test_voice_empty_speech_reprompts(client):
    _voice(client, 'CA-test-5')
    xml = _voice(client, 'CA-test-5', speech='')
    assert "didn't catch that" in xml and '<Gather' in xml


def test_voice_rejects_spoofed_request_when_token_configured(client, monkeypatch):
    # With an auth token set (production posture), an unsigned POST to /voice
    # is a spoof and must be rejected 403 before any session is opened.
    monkeypatch.setenv('TWILIO_AUTH_TOKEN', '0' * 32)
    r = client.post('/voice', data={'CallSid': 'CA-spoof'})
    assert r.status_code == 403


# -- Twilio Media Streams WebSocket (Streaming-STT path) ----------------------

def test_voice_greeting_uses_configured_persona():
    from gateway import VoicePersona
    from runtime.app import create_app
    app = create_app(persona=VoicePersona(name='Sam at the parts desk'))
    xml = TestClient(app).post('/voice', data={'CallSid': 'CA-persona'}).text
    assert 'Sam at the parts desk' in xml


def test_voice_stream_speaks_persona_greeting_on_start():
    import json

    from gateway import SimulatedStreamingASR, SimulatedTTS, VoicePersona
    from runtime.app import create_app
    app = create_app(streaming_asr=SimulatedStreamingASR(), tts=SimulatedTTS(),
                     persona=VoicePersona(name='Sam'))
    with TestClient(app).websocket_connect('/voice-stream') as ws:
        ws.send_text(json.dumps({'event': 'start',
                     'start': {'callSid': 'CA-g', 'streamSid': 'MZ9'}}))
        # greeting is spoken immediately: media frame(s) then a 'greeting' mark
        first = json.loads(ws.receive_text())
        mark = None
        for _ in range(2000):
            m = json.loads(ws.receive_text())
            if m['event'] == 'mark':
                mark = m
                break
        ws.send_text(json.dumps({'event': 'stop'}))
    assert first['event'] == 'media'
    assert mark and mark['mark']['name'] == 'greeting'


def test_voice_stream_feeds_continuous_improvement():
    # The always-on self-improvement loop runs on the agent's own call audio:
    # an uncertain transcript becomes a review opportunity after the call.
    import base64
    import json

    from gateway_fixtures import _shared_catalog

    from gateway import (
        ContinuousImprovement,
        CorrectionStore,
        ShadowObserver,
        SimulatedStreamingASR,
        SimulatedTTS,
        Transcript,
    )
    from resolution import ResolutionService
    from runtime.app import create_app
    from sku_translator import InMemoryStore

    cat, ver = _shared_catalog()
    svc = ResolutionService(cat, InMemoryStore(), catalog_version=ver)
    corr = CorrectionStore(cat)
    improvement = ContinuousImprovement(
        ShadowObserver(svc, catalog=cat, corrections=corr), corr, review_every=1)
    # caller says something the agent can't resolve cleanly
    asr = SimulatedStreamingASR(
        script=[Transcript(text='do you stock the qq9zz adapter', confidence=0.9)],
        bytes_per_turn=160)
    app = create_app(streaming_asr=asr, tts=SimulatedTTS(), improvement=improvement)
    with TestClient(app).websocket_connect('/voice-stream') as ws:
        ws.send_text(json.dumps({'event': 'start',
                     'start': {'callSid': 'CA-imp', 'streamSid': 'MZ1'}}))
        for _ in range(2000):
            m = json.loads(ws.receive_text())
            if m.get('event') == 'mark' and m['mark']['name'] == 'greeting':
                break
        ws.send_text(json.dumps({'event': 'media', 'streamSid': 'MZ1',
                     'media': {'payload': base64.b64encode(b'\xff' * 160).decode()}}))
        ws.receive_json()                       # assistant event
        ws.send_text(json.dumps({'event': 'stop'}))
    # after the call, the uncertain moment is queued for periodic HITL review
    assert improvement.calls == 1
    assert improvement.pending_review().opportunities


def test_shadow_stream_endpoint_observes_and_learns():
    # Observe-only ride-along over the real WS surface: dual-channel audio ->
    # transcript -> continuous loop -> a rep resolution is harvested live.
    import base64
    import json

    from gateway_fixtures import _shared_catalog

    from gateway import (
        ContinuousImprovement,
        CorrectionStore,
        ShadowObserver,
        SimulatedStreamingASR,
        Transcript,
    )
    from resolution import ResolutionService
    from runtime.app import create_app
    from sku_translator import InMemoryStore

    cat, ver = _shared_catalog()
    corr = CorrectionStore(cat)
    svc = ResolutionService(cat, InMemoryStore(), catalog_version=ver,
                            learned_aliases=corr)
    ci = ContinuousImprovement(
        ShadowObserver(svc, catalog=cat, corrections=corr), corr, review_every=99)
    scripts = {'inbound': [Transcript('do you stock the qq9zz adapter', 0.9)],
               'outbound': [Transcript('that is K5-24SBC', 0.9)]}

    class _TrackASR:
        def open(self, *, sample_rate, encoding, keyterms=None):
            # the bridge opens one session per track, inbound first
            track = 'inbound' if not getattr(self, '_o', False) else 'outbound'
            self._o = True
            return SimulatedStreamingASR(script=scripts[track],
                                         bytes_per_turn=160).open(
                sample_rate=sample_rate, encoding=encoding)

    app = create_app(streaming_asr=_TrackASR(), improvement=ci)
    pay = base64.b64encode(b'\xff' * 160).decode()
    with TestClient(app).websocket_connect('/shadow-stream') as ws:
        ws.send_text(json.dumps({'event': 'start',
                     'start': {'callSid': 'CA-sh', 'streamSid': 'MZ1'}}))
        ws.send_text(json.dumps({'event': 'media', 'sequenceNumber': '1',
                     'media': {'track': 'inbound', 'payload': pay}}))
        ws.send_text(json.dumps({'event': 'media', 'sequenceNumber': '2',
                     'media': {'track': 'outbound', 'payload': pay}}))
        ws.send_text(json.dumps({'event': 'stop'}))
    # autonomous ride-along PROPOSES (gated), not instant-live
    from gateway.alias_store import PROPOSED
    a = corr.get_alias('do you stock the qq9zz adapter')
    assert a is not None and a.state == PROPOSED and a.target_sku == 'K5-24SBC'
    assert corr.alias_for('do you stock the qq9zz adapter') is None  # gate holds


def test_voice_stream_websocket_full_duplex():
    import base64
    import json

    from gateway import SimulatedStreamingASR, SimulatedTTS, Transcript
    from runtime.app import create_app

    # Scripted ASR + simulated TTS => no audio/credentials needed.
    asr = SimulatedStreamingASR(
        script=[Transcript(text='K5-24SBC', confidence=0.9)], bytes_per_turn=160)
    app = create_app(streaming_asr=asr, tts=SimulatedTTS())
    with TestClient(app).websocket_connect('/voice-stream') as ws:
        ws.send_text(json.dumps({'event': 'start',
                     'start': {'callSid': 'CA-ws-1', 'streamSid': 'MZ1'}}))
        # drain the opening greeting (media frames + a 'greeting' mark)
        for _ in range(2000):
            m = json.loads(ws.receive_text())
            if m.get('event') == 'mark' and m['mark']['name'] == 'greeting':
                break
        payload = base64.b64encode(b'\xff' * 160).decode()
        ws.send_text(json.dumps({'event': 'media', 'streamSid': 'MZ1',
                     'media': {'payload': payload}}))
        # next JSON message is the assistant event, then the spoken reply frames
        msg = ws.receive_json()
        outbound = [json.loads(ws.receive_text()) for _ in range(2)]
        ws.send_text(json.dumps({'event': 'stop'}))
    assert msg['type'] == 'assistant' and msg['heard'] == 'K5-24SBC'
    assert 'K5' in msg['text'] or '?' in msg['text']   # gate intact
    events = {o['event'] for o in outbound}
    assert 'media' in events                          # audio played back to caller
    media = next(o for o in outbound if o['event'] == 'media')
    assert media['streamSid'] == 'MZ1' and media['media']['payload']


def test_to_spoken_normalizes_dimensions_for_speech():
    from gateway.spoken import spoken_description, to_spoken
    assert to_spoken('5"X24') == '5 by 24 inch'
    assert to_spoken('5" x 24"') == '5 by 24 inch'
    assert to_spoken('5x24') == '5 by 24 inch'
    assert to_spoken('24"') == '24 inch'
    # clean prose / dates / prices pass through untouched
    assert to_spoken('ships on 2026-06-10 for $12.50') == 'ships on 2026-06-10 for $12.50'
    assert spoken_description(None) is None
