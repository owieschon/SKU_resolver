"""Non-live parsing tests for the credentialed seams.

These seams (AssemblyAI streaming, Anthropic/OpenAI providers) otherwise only
run under live credentials. Their *parsing* — the part most likely to break on
a real payload — is extracted into pure functions and tested here against
synthetic API responses, so a parse regression is caught in CI, not in prod.
Demonstrate-the-catch: every case includes a malformed/degenerate payload.
"""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

from model_provider.base import ModelRequest
from model_provider.anthropic_provider import parse_anthropic_response
from model_provider.openai_compat import parse_openai_response
from gateway.asr_streaming import parse_turn_message


# --- AssemblyAI v3 Turn messages -----------------------------------------------

def _turn(transcript, end=True, words=None, type_='Turn'):
    msg = {'type': type_, 'end_of_turn': end, 'transcript': transcript}
    if words is not None:
        msg['words'] = words
    return json.dumps(msg)


def test_turn_finalized_with_word_confidences_averages():
    t = parse_turn_message(_turn('K5 24 SBC', words=[
        {'text': 'K5', 'confidence': 0.9}, {'text': '24', 'confidence': 0.7}]))
    assert t is not None and t.is_final and t.text == 'K5 24 SBC'
    assert abs(t.confidence - 0.8) < 1e-9


def test_turn_finalized_without_words_defaults_full_confidence():
    t = parse_turn_message(_turn('hello'))
    assert t is not None and t.confidence == 1.0


def test_partial_turn_is_not_final_returns_none():
    assert parse_turn_message(_turn('partial', end=False)) is None


def test_non_turn_messages_return_none():
    assert parse_turn_message(json.dumps({'type': 'Begin'})) is None
    assert parse_turn_message(json.dumps({'type': 'Termination'})) is None


def test_malformed_or_nonobject_payloads_return_none():
    assert parse_turn_message('not json') is None
    assert parse_turn_message('[1,2,3]') is None
    assert parse_turn_message(b'\x00\x01') is None


def test_turn_missing_transcript_is_empty_string():
    t = parse_turn_message(json.dumps({'type': 'Turn', 'end_of_turn': True}))
    assert t is not None and t.text == ''


# --- AssemblyAI adapter construction / query string (no network) ---------------

def test_assemblyai_requires_key(monkeypatch):
    monkeypatch.delenv('ASSEMBLYAI_API_KEY', raising=False)
    from gateway.asr_streaming import AssemblyAIStreamingASR
    import pytest
    with pytest.raises(RuntimeError):
        AssemblyAIStreamingASR()


def test_assemblyai_querystring_percent_encodes_values():
    from gateway.asr_streaming import _querystring
    qs = _querystring({'sample_rate': 8000, 'encoding': 'pcm_mulaw',
                       'keyterms_prompt': 'curved stack,K5-24SBC'})
    assert 'sample_rate=8000' in qs
    assert 'encoding=pcm_mulaw' in qs
    # spaces and commas are percent-encoded, not left raw
    assert 'curved%20stack%2CK5-24SBC' in qs


# --- Anthropic Messages response -----------------------------------------------

def _req(schema=None):
    return ModelRequest(task='intent', model='claude-haiku-4-5', system='s',
                        user='u', json_schema=schema, max_tokens=64)


def test_anthropic_extracts_text_and_skips_nontext_blocks():
    msg = NS(content=[NS(type='thinking', text='ignore'),
                      NS(type='text', text='the answer')],
             usage=NS(input_tokens=100, output_tokens=20))
    r = parse_anthropic_response(msg, _req())
    assert r.text == 'the answer' and r.in_tokens == 100 and r.out_tokens == 20
    # cost from the 1M-token table: 100/1e6*1 + 20/1e6*5
    assert r.cost_usd == round(100 / 1e6 * 1.0 + 20 / 1e6 * 5.0, 6)


def test_anthropic_structured_output_decoded():
    msg = NS(content=[NS(type='text', text='{"intent": "availability"}')],
             usage=NS(input_tokens=10, output_tokens=5))
    r = parse_anthropic_response(msg, _req(schema={'type': 'object'}))
    assert r.data == {'intent': 'availability'}


def test_anthropic_tolerates_empty_content_and_missing_usage():
    r = parse_anthropic_response(NS(content=[], usage=None), _req())
    assert r.text == '' and r.in_tokens == 0 and r.out_tokens == 0


def test_anthropic_bad_json_structured_output_is_none_not_crash():
    msg = NS(content=[NS(type='text', text='not json')],
             usage=NS(input_tokens=1, output_tokens=1))
    r = parse_anthropic_response(msg, _req(schema={'type': 'object'}))
    assert r.data is None and r.text == 'not json'


# --- OpenAI-compatible Chat Completions response -------------------------------

def _oai(content, prompt=12, completion=8):
    return NS(choices=[NS(message=NS(content=content))],
              usage=NS(prompt_tokens=prompt, completion_tokens=completion))


def test_openai_extracts_content_and_cost():
    req = ModelRequest(task='intent', model='gpt-5-mini', system='s', user='u',
                       json_schema=None, max_tokens=64)
    r = parse_openai_response(_oai('hi'), req, provider='openai')
    assert r.text == 'hi' and r.in_tokens == 12 and r.out_tokens == 8
    assert r.cost_usd > 0   # no longer silently zero (was a real gap)


def test_openai_structured_output_decoded():
    r = parse_openai_response(_oai('{"k": 1}'), _req(schema={'type': 'object'}))
    assert r.data == {'k': 1}


def test_openai_tolerates_no_choices_and_null_content():
    assert parse_openai_response(NS(choices=[], usage=None), _req()).text == ''
    assert parse_openai_response(_oai(None), _req()).text == ''
