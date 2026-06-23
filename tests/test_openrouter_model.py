"""The real model_fn's pure surface — mapping the OpenAI SDK message into the
brain's conformant return shape, and the system-prompt injection contract — is
tested with a FAKE client (no network). The live adversarial run
(scripts/adversarial_live.py) exercises the network path against the real model;
these lock the seam the brain's proofs assume: text->str, tool_calls->{'tool_call'}.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from runtime.openrouter_model import (
    RESOLVE_PART_TOOL,
    make_model_fn,
    production_system_prompt,
)


class _FakeCompletions:
    def __init__(self, message, capture):
        self._message = message
        self._capture = capture

    def create(self, **kwargs):
        self._capture.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=self._message)])


class _FakeClient:
    def __init__(self, message):
        self.capture = {}
        self.chat = SimpleNamespace(
            completions=_FakeCompletions(message, self.capture))


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def test_text_reply_maps_to_str():
    client = _FakeClient(_msg(content='hi there'))
    fn = make_model_fn(client=client, inject_system=False)
    assert fn([{'role': 'user', 'content': 'hello'}]) == 'hi there'


def test_tool_call_maps_to_conformant_shape():
    tc = SimpleNamespace(id='call_1', function=SimpleNamespace(
        name='resolve_part', arguments='{"text": "K5-24SBC"}'))
    client = _FakeClient(_msg(content=None, tool_calls=[tc]))
    fn = make_model_fn(client=client, inject_system=False)
    out = fn([{'role': 'user', 'content': 'stock?'}])
    assert out == {'tool_call': [{'id': 'call_1', 'type': 'function',
                                  'function': {'name': 'resolve_part',
                                               'arguments': '{"text": "K5-24SBC"}'}}]}


def test_null_content_maps_to_empty_string():
    client = _FakeClient(_msg(content=None, tool_calls=None))
    fn = make_model_fn(client=client, inject_system=False)
    assert fn([{'role': 'user', 'content': 'x'}]) == ''


def test_system_prompt_injected_when_absent():
    client = _FakeClient(_msg(content='ok'))
    fn = make_model_fn(client=client, inject_system=True)
    fn([{'role': 'user', 'content': 'hello'}])
    roles = [m['role'] for m in client.capture['messages']]
    assert roles[0] == 'system'
    assert 'parts' in client.capture['messages'][0]['content'].lower()


def test_system_prompt_not_double_injected():
    client = _FakeClient(_msg(content='ok'))
    fn = make_model_fn(client=client, inject_system=True)
    fn([{'role': 'system', 'content': 'caller-supplied system'},
        {'role': 'user', 'content': 'hello'}])
    roles = [m['role'] for m in client.capture['messages']]
    assert roles.count('system') == 1
    assert client.capture['messages'][0]['content'] == 'caller-supplied system'


def test_tool_and_temperature_are_passed():
    client = _FakeClient(_msg(content='ok'))
    fn = make_model_fn(client=client, inject_system=False)
    fn([{'role': 'user', 'content': 'x'}])
    assert client.capture['tools'] == [RESOLVE_PART_TOOL]
    assert client.capture['temperature'] == 0.0


def test_tool_schema_is_text_only_no_caller_id():
    # caller_id is an ElevenLabs dynamic variable; exposing it to the model would
    # invite an invented session key. The model-facing schema must be text-only.
    props = RESOLVE_PART_TOOL['function']['parameters']['properties']
    assert set(props) == {'text'}
    assert RESOLVE_PART_TOOL['function']['parameters']['required'] == ['text']


def test_missing_key_raises_at_build_time(monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    with pytest.raises(RuntimeError):
        make_model_fn()


def test_production_prompt_loads_and_has_guardrails():
    p = production_system_prompt()
    assert 'Never invent part facts' in p
