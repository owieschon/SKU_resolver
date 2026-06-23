"""Edge-case + property hardening for the newer modules.

Degenerate inputs (empty/all-numeric/single-member catalogs, malformed audio
frames, empty worksheets) and a couple of exhaustive property checks. The point
is that the happy-path tests elsewhere don't lull us — these are the inputs a
real, messy tenant actually sends.
"""
from __future__ import annotations

import json

from erp_harness.catalog_source import (
    rows_from_catalog_lines,
    rows_from_html_tables,
    rows_from_worksheet,
)
from erp_harness.grammar_induction import (
    CatalogGrammarReport,
    decode_catalog,
    normalize_sku,
    segment,
)
from gateway.voice_stream import TwilioMediaStream, mulaw_decode, parse_twilio_event

# --- grammar induction: degenerate catalogs ------------------------------------

def test_decode_empty_catalog_is_safe():
    r = decode_catalog([], sku_field='sku', description_field='description')
    assert isinstance(r, CatalogGrammarReport)
    assert r.total_items == 0 and r.families == ()
    assert r.structured_share == 0.0 and r.roled_share == 0.0
    assert r.residual_recommendation       # still explains itself


def test_decode_all_numeric_prefix_finds_no_families():
    rows = [{'sku': f'{1000 + i}', 'description': 'legacy part'}
            for i in range(20)]
    r = decode_catalog(rows, sku_field='sku', description_field='description')
    assert r.families == ()                 # no alpha prefix -> no families
    assert 'convention review' in r.residual_recommendation


def test_single_member_family_is_residual_not_confident():
    rows = [{'sku': 'ZZ1', 'description': 'one off'}]
    r = decode_catalog(rows, sku_field='sku', description_field='description')
    assert 'ZZ' not in {f.family_code for f in r.families}
    assert any('ZZ' in q.question for q in r.sme_questions)


# --- catalog_source: empty / malformed ----------------------------------------

def test_worksheet_empty_and_header_only():
    assert rows_from_worksheet([]) == []
    assert rows_from_worksheet([['SKU', 'Description']]) == []


def test_catalog_lines_with_no_codes_yield_nothing():
    assert rows_from_catalog_lines(['just some words', 'TABLE OF CONTENTS']) == []


def test_html_with_no_table_yields_nothing():
    assert rows_from_html_tables('<html><body><p>no tables</p></body></html>') == []


# --- voice_stream: malformed / non-media frames --------------------------------

def test_parse_unknown_and_stop_events():
    assert parse_twilio_event(json.dumps({'event': 'mark'})).event == 'mark'
    assert parse_twilio_event(json.dumps({'event': 'stop'})).event == 'stop'
    # media frame with no payload -> empty audio, not a crash
    ev = parse_twilio_event(json.dumps({'event': 'media', 'media': {}}))
    assert ev.event == 'media' and ev.mulaw == b''


def test_media_before_start_still_accumulates_without_call_sid():
    s = TwilioMediaStream()
    import base64
    s.feed(json.dumps({'event': 'media',
                       'media': {'payload': base64.b64encode(b'\xff' * 10).decode()}}))
    assert s.call_sid is None and len(s.mulaw) == 10


def test_mulaw_decode_empty_is_empty():
    assert mulaw_decode(b'') == b''


# --- property checks ------------------------------------------------------------

def test_mulaw_decode_all_bytes_in_int16_range():
    pcm = mulaw_decode(bytes(range(256)))
    import struct
    samples = struct.unpack('<256h', pcm)
    assert all(-32768 <= s <= 32767 for s in samples)
    assert samples[0xFF] == 0           # mu-law zero code decodes to silence
    assert samples[0x00] == -samples[0x80]   # symmetric extremes


def test_segmentation_is_lossless_roundtrip():
    for sku in ['K5-24SBC', 'WA902-01-1002', '  zx4/100c ', '0199-LL2-003',
                'A', '12345', '-S4M-', 'BH5x2']:
        assert ''.join(s.text for s in segment(sku)) == normalize_sku(sku)
