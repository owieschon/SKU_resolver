"""The ElevenLabs agent definition is a safety artifact: a hosted model speaks
to customers, and the only thing keeping it from inventing a part or a price is
(1) the gateway tool it must call and (2) the guardrails in its system prompt.
These tests pin both — and prove the check catches the fault: a prompt that loses a
guardrail FAILS validation, it does not ship quietly.
"""
from __future__ import annotations

from gateway import VoicePersona
from runtime.voice_agent import (
    AgentSettings, asr_keywords_from_skus, build_agent_payload, guardrails_config,
    load_system_prompt, resolve_part_tool, system_tools, validate_system_prompt,
)


# -- the shipped prompt is valid ---------------------------------------------

def test_shipped_prompt_has_every_required_guardrail():
    assert validate_system_prompt(load_system_prompt()) == []


def test_prompt_has_all_six_blocks():
    t = load_system_prompt().lower()
    for block in ('# personality', '# environment', '# tone', '# goal',
                  '# guardrails', '# tools'):
        assert block in t


# -- prove the check catches the fault: drop a guardrail -> validation fails -------------

def test_removing_never_invent_clause_is_caught():
    prompt = load_system_prompt()
    assert 'Never invent' in prompt
    tampered = prompt.replace('Never invent', 'Sure, go ahead and guess')
    missing = validate_system_prompt(tampered)
    assert 'never-invent part facts' in missing


def test_removing_pricing_gate_is_caught():
    prompt = load_system_prompt().replace('Pricing is gated', 'Pricing is open')
    assert 'pricing gated behind verification' in validate_system_prompt(prompt)


def test_dropping_guardrails_heading_is_caught():
    prompt = load_system_prompt().replace('# Guardrails', '# Notes')
    missing = validate_system_prompt(prompt)
    assert 'six-block: Guardrails heading' in missing


# -- the tool wiring ----------------------------------------------------------

def test_resolve_part_tool_targets_agent_turn():
    tool = resolve_part_tool('https://host.example/')
    assert tool['type'] == 'webhook'
    assert tool['name'] == 'resolve_part'
    assert tool['api_schema']['url'] == 'https://host.example/agent/turn'
    assert tool['api_schema']['method'] == 'POST'
    # POST webhook bodies use request_body_schema (the live API rejects
    # body_params_schema for POST).
    props = tool['api_schema']['request_body_schema']['properties']
    assert 'text' in props                       # LLM-extracted utterance
    # caller_id is bound to the conversation id via a system dynamic variable,
    # not invented by the LLM.
    assert props['caller_id']['dynamic_variable'] == 'system__conversation_id'
    # latency UX: acknowledge while the lookup runs, and cap a hung call
    assert tool['pre_tool_speech'] in ('auto', 'force', 'off')
    assert tool['response_timeout_secs'] >= 5
    # no auth header unless a secret id is supplied
    assert 'X-Agent-Token' not in tool['api_schema']['request_headers']


def test_resolve_part_tool_carries_secret_auth_header_when_configured():
    # Containment: the auth header value is a workspace-secret reference
    # ({secret_id}), never a literal token in the agent config.
    tool = resolve_part_tool('https://h.example', auth_secret_id='sec_123')
    hdr = tool['api_schema']['request_headers']['X-Agent-Token']
    assert hdr == {'secret_id': 'sec_123'}


# -- system tools (escalation must actually do something) --------------------

def test_end_call_always_present_transfer_only_with_number():
    # API-created agents get NO system tools by default; we add end_call always.
    none = system_tools('')
    assert [t['name'] for t in none] == ['end_call']
    withnum = system_tools('+15551234567')
    names = [t['name'] for t in withnum]
    assert 'end_call' in names and 'transfer_to_number' in names
    transfer = next(t for t in withnum if t['name'] == 'transfer_to_number')
    dest = transfer['params']['transfers'][0]
    assert dest['transferDestination']['phoneNumber'] == '+15551234567'
    assert dest['transferType'] == 'conference'      # warm transfer


# -- ASR keyword biasing derived from the real catalog -----------------------

def test_asr_keywords_extracts_prefixes_and_suffixes():
    skus = ['K5-24SBC', 'K5-24EXC', 'R5-4C', 'L590-1515SC', 'BH5-36SBC']
    kw = asr_keywords_from_skus(skus, limit=50)
    assert 'K5' in kw and 'R5' in kw and 'L590' in kw       # family prefixes
    assert 'SBC' in kw and 'EXC' in kw and 'SC' in kw       # finish/body codes
    assert len(kw) == len(set(kw))                          # deduped


# -- the assembled payload ----------------------------------------------------

def test_payload_sources_voice_and_greeting_from_persona():
    persona = VoicePersona(name='Tenant-001 parts', accent='midwest',
                           greeting='Parts department, how can I help?')
    payload = build_agent_payload(persona=persona,
                                  tool_base_url='https://h.example')
    agent = payload['conversation_config']['agent']
    assert agent['first_message'] == 'Parts department, how can I help?'
    # midwest persona -> its resolved voice id, the one source of truth
    assert (payload['conversation_config']['tts']['voice_id']
            == persona.resolved_voice_id())
    # resolve_part tool present (plus system tools), pointing at our gateway
    tools = agent['prompt']['tools']
    names = [t['name'] for t in tools]
    assert 'resolve_part' in names and 'end_call' in names


def test_payload_carries_asr_turn_and_guardrails():
    s = AgentSettings(asr_keywords=('K5', 'SBC'), transfer_number='+15551112222')
    payload = build_agent_payload(persona=VoicePersona(),
                                  tool_base_url='https://h.example', settings=s)
    cc = payload['conversation_config']
    assert cc['asr']['provider'] == 'scribe_realtime'
    assert cc['asr']['user_input_audio_format'] == 'ulaw_8000'
    assert cc['asr']['keywords'] == ['K5', 'SBC']
    assert cc['turn']['turn_eagerness'] == 'patient'            # don't cut callers off
    assert cc['turn']['soft_timeout_config']['message']        # latency filler
    # platform guardrails present with the version "1" discriminator
    g = payload['platform_settings']['guardrails']
    assert g['version'] == '1'


def test_guardrails_config_is_version_1_with_parts_only_custom_rule():
    g = guardrails_config()
    assert g['version'] == '1'                                  # the 422 fix
    assert g['focus']['isEnabled'] and g['prompt_injection']['isEnabled']
    custom = g['custom']['config']['configs']
    rule = next(c for c in custom if c['name'] == 'parts_only_from_tool')
    assert rule['model'] == 'gemini-2.5-flash-lite'
    assert rule['execution_mode'] == 'blocking'


def test_settings_speed_is_clear_not_rushed():
    # codes read clearer a touch under 1.0; never robotic-fast
    assert 0.9 <= AgentSettings().speed < 1.0
    assert AgentSettings().style == 0.0                        # style adds latency
