"""FastAPI runtime — the deployed surface (P4).

Endpoints:
  POST /v1/sessions            -> open a session, returns {session_id, token}
  POST /v1/turns               -> one conversational turn (chat channel)
  POST /v1/sessions/{id}/verify-> account verification
  GET  /v1/tools.json          -> the function-calling manifest (CS platforms)
  GET  /openapi.json           -> FastAPI's generated OpenAPI (G1 contract)
  POST /voice                  -> Twilio voice webhook (TwiML <Gather> flow)
  POST /webhook                -> signed inbound webhook (HMAC + replay)

Config selects scripted-vs-real adapters (runtime/config.py); the app boots
and serves with local defaults and zero external dependencies. The voice
endpoint maps a Twilio CallSid to a gateway session so a phone call is just
a sequence of turns on the VOICE channel — the SAME gates and never-invent
guarantee as chat.
"""
# NB: no `from __future__ import annotations` — FastAPI must see real types
# (notably `Request`) at decoration time, not stringized annotations.
import os
from typing import Dict, Tuple

from gateway import Channel, tools_manifest
from gateway.connector import _response_to_dict
from gateway.say_guard import safe_voice_say
from gateway.spoken import to_spoken
from gateway.voice import transcript_is_usable
from runtime import twilio_sig, twiml
from runtime.config import (
    build_gateway,
    build_improvement,
    build_persona,
    build_streaming_asr,
    build_tts,
)


def create_app(*, streaming_asr=None, tts=None, persona=None, improvement=None):
    from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse

    from observability import init_error_tracking
    init_error_tracking()   # Sentry iff SENTRY_DSN is set + sentry-sdk installed; else no-op

    app = FastAPI(title='SKU Resolution Gateway', version='1.0.0')
    gateway, sessions = build_gateway()
    # Voice persona (name/accent/voice/greeting) + ASR/TTS (injectable for tests).
    persona = persona if persona is not None else build_persona()
    asr = streaming_asr if streaming_asr is not None else build_streaming_asr()
    tts_engine = tts if tts is not None else build_tts(persona)
    # Always-on self-monitoring of the agent's own calls (off unless configured).
    improvement = (improvement if improvement is not None
                   else build_improvement(gateway))
    # CallSid -> (session_id, token) for the voice channel.
    _calls: Dict[str, Tuple[str, str]] = {}

    @app.post('/v1/sessions')
    def open_session(channel_id: str = 'api'):
        token = sessions.open(channel_id, channel_id)
        return {'session_id': channel_id, 'token': token}

    @app.post('/v1/turns')
    async def turn(request: Request):
        body = await request.json()
        resp = gateway.turn(body['session_id'], body['token'], body['text'],
                            channel=Channel(body.get('channel', 'typed')))
        return _response_to_dict(resp)

    @app.post('/v1/sessions/{session_id}/verify')
    async def verify(session_id: str, request: Request):
        body = await request.json()
        # route a verify-shaped turn through the gateway so all gates apply
        text = (f"account number {body['account_no']}" if body.get('account_no')
                else f"account name {body.get('name', '')}")
        resp = gateway.turn(session_id, body['token'], text,
                            channel=Channel.TYPED)
        return _response_to_dict(resp)

    @app.get('/v1/tools.json')
    def tools():
        return tools_manifest()

    # -- voice-agent tool: the deterministic gateway as a callable tool --------
    # A hosted voice agent (e.g. ElevenLabs Agents) handles speech, small talk,
    # and turn-taking; for ANY part/availability/price question it calls this
    # tool, which runs the SAME gated deterministic gateway and returns a
    # verbatim `say` string for the agent to speak. Never-invent + pricing-
    # behind-verification stay enforced HERE, in code — the agent can't get a
    # SKU or a price except through this tool. Session is keyed by caller_id so
    # verification persists across the call.
    _agent_calls: Dict[str, Tuple[str, str]] = {}

    @app.post('/agent/turn')
    async def agent_turn(request: Request):
        # Containment (environment layer): the tool is the agent's ENTIRE
        # capability surface, so only the configured agent may call it. When
        # AGENT_TOOL_SECRET is set, require a matching X-Agent-Token header
        # (the agent sends it from a workspace secret). Fails open only when no
        # secret is configured — local/dev — mirroring twilio_sig. This closes
        # the open-tunnel hole: anyone who finds the URL can no longer query it.
        import hmac
        expected = os.environ.get('AGENT_TOOL_SECRET')
        if expected and not hmac.compare_digest(
                request.headers.get('X-Agent-Token', ''), expected):
            return JSONResponse({'error': 'forbidden'}, status_code=403)
        body = await request.json()
        caller = str(body.get('caller_id') or 'agent-default')
        text = str(body.get('text') or '')
        if caller not in _agent_calls:
            tok = sessions.open(caller, f'agent:{caller}')
            _agent_calls[caller] = (caller, tok)
        sid, tok = _agent_calls[caller]
        # The /agent/turn path runs the ORCHESTRATION backend (converse), not the
        # legacy fixed-sequence turn(). Legacy stays only on the other channels
        # (/voice, /v1/turns); it is NOT a fallback here — converse fails closed to
        # a coherent escalation, never reverts to legacy determinism.
        resp = gateway.converse(sid, tok, text, channel=Channel.TYPED)
        # Capture the same self-improvement data as the voice path (off unless
        # SKU_IMPROVEMENT is configured): the agent path feeds the loop too.
        if improvement is not None:
            try:
                improvement.observe_agent_turn(text)
            except Exception:
                pass
        d = _response_to_dict(resp)
        # Structural provenance for the containment router: which SKUs/values this
        # turn DISCLOSED. The router uses these (never the prose) to decide fact-
        # turn (verbatim say) vs free-turn. assert_complete guarantees a disclosure
        # can't under-report and masquerade as a free turn.
        from gateway.provenance import assert_complete, surfaced
        assert_complete(resp)
        surfaced_skus, surfaced_values = surfaced(resp)
        # `say` is the exact text the agent must speak verbatim, rendered for the
        # ear: dimensions normalized and part numbers spelled ("K 5, 24 S B C").
        return {'say': safe_voice_say(resp.text), 'kind': resp.kind,
                'surfaced_skus': list(surfaced_skus),
                'surfaced_values': surfaced_values,
                'session_state': d.get('session_state'),
                'needs_confirmation': d.get('needs_confirmation', False),
                'refused': d.get('refused')}

    # -- Twilio voice (TwiML <Gather> request/response) -----------------------

    @app.post('/voice')
    async def voice(request: Request):
        form = await request.form()
        # Reject spoofed requests: when TWILIO_AUTH_TOKEN is set (production),
        # the X-Twilio-Signature must validate. Fails open only in local/ngrok
        # dev where no token is configured (see runtime.twilio_sig).
        if not twilio_sig.validate(
                str(request.url), {k: str(v) for k, v in form.items()},
                request.headers.get('X-Twilio-Signature', '')):
            return Response('forbidden', status_code=403)
        call_sid = form.get('CallSid', 'unknown')
        speech = (form.get('SpeechResult') or '').strip()
        action = '/voice'

        if call_sid not in _calls:
            token = sessions.open(call_sid, f'twilio:{call_sid}')
            _calls[call_sid] = (call_sid, token)
            return Response(twiml.say_and_gather(to_spoken(persona.opening()), action),
                            media_type='application/xml')

        sid, token = _calls[call_sid]
        if not speech:
            return Response(twiml.say_and_gather(
                "Sorry, I didn't catch that. What part are you looking for?",
                action), media_type='application/xml')

        resp = gateway.turn(sid, token, speech, channel=Channel.VOICE)
        if resp.kind == 'escalate':
            return Response(twiml.dial_agent(
                resp.text, os.environ.get('VOICE_TRANSFER_NUMBER')),
                media_type='application/xml')
        return Response(twiml.say_and_gather(safe_voice_say(resp.text), action),
                        media_type='application/xml')

    # -- Twilio Media Streams (Streaming-STT fidelity path) -------------------

    @app.post('/voice-entry')
    async def voice_entry(request: Request):
        """Entry TwiML for the streaming path: tell Twilio to open a Media
        Stream to /voice-stream (the wss host is this request's host, so it
        works behind a tunnel). The stream handler speaks the ElevenLabs
        greeting on start, so no <Say> greeting here."""
        host = request.headers.get('host', '')
        return Response(twiml.connect_stream(f'wss://{host}/voice-stream'),
                        media_type='application/xml')

    @app.websocket('/voice-stream')
    async def voice_stream(ws: WebSocket):
        """Ingest Twilio Media Streams audio, transcribe via the streaming ASR,
        and run a gateway turn per finalized utterance. The transcription source
        improves; every gate is unchanged (the gateway is still the brain).
        It speaks each gated reply back as audio (TTS -> mu-law -> Twilio media
        frames) for full duplex, and also emits a JSON 'assistant' event for
        transcript/supervisor use. The gateway is still the brain — every gate
        applies before a single audio frame is synthesized."""
        from gateway.voice_stream import TwilioMediaStream, twilio_mark, twilio_media_messages
        await ws.accept()
        stream = TwilioMediaStream()
        session = None
        sid = token = None
        turn_no = 0
        live_turns = []        # the agent's own call, for self-monitoring
        try:
            while True:
                raw = await ws.receive_text()
                ev = stream.feed(raw)
                if ev.event == 'start':
                    sid = stream.call_sid or 'unknown'
                    token = sessions.open(sid, f'twilio:{sid}')
                    session = asr.open(sample_rate=8000, encoding='pcm_mulaw')
                    # Speak the persona greeting as soon as the stream opens.
                    target = stream.stream_sid or sid
                    for frame in twilio_media_messages(
                            tts_engine.synthesize(to_spoken(persona.opening())), target):
                        await ws.send_text(frame)
                    await ws.send_text(twilio_mark(target, 'greeting'))
                elif ev.event == 'media' and session is not None:
                    session.feed(ev.mulaw)
                    for t in session.drain():
                        if not transcript_is_usable(t):
                            continue
                        live_turns.append(('customer', t.text))  # self-monitor buffer
                        # Per-turn isolation: one bad turn must not drop the call.
                        try:
                            resp = gateway.turn(sid, token, t.text,
                                                channel=Channel.VOICE)
                            await ws.send_json({
                                'type': 'assistant', 'kind': resp.kind,
                                'text': resp.text, 'heard': t.text})
                            # Reply leg: synthesize the EXACT reply text
                            # (TTS returns mu-law @ 8kHz) and play it back.
                            target = stream.stream_sid or sid
                            mulaw = tts_engine.synthesize(safe_voice_say(resp.text))
                            for frame in twilio_media_messages(mulaw, target):
                                await ws.send_text(frame)
                            turn_no += 1
                            await ws.send_text(
                                twilio_mark(target, f'reply-{turn_no}'))
                        except WebSocketDisconnect:
                            raise
                        except Exception:
                            # Degrade gracefully: keep the call alive instead of
                            # dropping it on one turn's failure.
                            await ws.send_text(
                                twilio_mark(stream.stream_sid or sid, 'error'))
                elif ev.event == 'stop':
                    break
        except WebSocketDisconnect:
            pass
        finally:
            if session is not None:
                session.close()
            # Flush the agent's own call to the always-on self-improvement loop:
            # it flags the uncertain moments for periodic human review. Read-only;
            # never changes behavior mid-call.
            if improvement is not None and live_turns:
                improvement.ingest_self_monitored_call(live_turns)

    # -- Shadow stream: observe-only ride-along on a real (dual-channel) call --

    @app.websocket('/shadow-stream')
    async def shadow_stream(ws: WebSocket):
        """Ride along on a real rep<->customer call (Twilio dual-channel) in
        OBSERVE-ONLY mode: transcribe each track, reconstruct a speaker-tagged
        transcript, and feed the continuous-improvement loop (harvests the rep's
        handling of anything the tool missed). Never speaks or acts."""
        from gateway import ShadowStreamBridge
        await ws.accept()
        bridge = ShadowStreamBridge(
            lambda track: asr.open(sample_rate=8000, encoding='pcm_mulaw'),
            improvement)
        try:
            while True:
                ev = bridge.feed(await ws.receive_text())
                if ev.event == 'stop':
                    break
        except WebSocketDisconnect:
            pass
        finally:
            bridge.finish()

    # -- signed inbound webhook (CS platforms) --------------------------------

    @app.post('/webhook')
    async def webhook(request: Request):
        import time

        from gateway.connector import WebhookConnector
        secret = os.environ.get('SKU_WEBHOOK_SECRET', 'dev-webhook-secret').encode()
        conn = WebhookConnector(gateway=gateway, secret=secret,
                                now_fn=time.time)
        raw = await request.body()
        sig = request.headers.get('X-Signature', '')
        nonce = request.headers.get('X-Nonce', '')
        ts = float(request.headers.get('X-Timestamp', '0') or 0)
        return JSONResponse(conn.handle(raw, sig, nonce=nonce, ts=ts))

    @app.get('/healthz')
    def health():
        # Liveness only — no security-posture details on a public endpoint.
        # Dev-secret / signature posture is surfaced by the deploy guard
        # (runtime/observability.deploy_guard), not advertised over HTTP.
        return {'ok': True}

    # The custom-LLM seam ElevenLabs points its Agent's LLM at: POST
    # /v1/chat/completions runs the containment brain (handle_async) over the live
    # async model, instrumented per-route from the first call. The model_fn builds
    # lazily from the OpenRouter key, so app boot needs no key and substitution
    # turns work without one.
    from runtime.custom_llm_route import register_custom_llm
    register_custom_llm(app)

    return app
