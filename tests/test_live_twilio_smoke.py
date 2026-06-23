"""Live Twilio smoke — verify the account credentials are real, read-only.

Credential-gated, no spend, no call placed: a GET on the account resource proves
the SID/token are live and the account is active. Placing/receiving an actual
call needs a human to dial (or a deployed public URL + number config) and is out
of scope for an automated smoke.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get('TWILIO_ACCOUNT_SID') and os.environ.get('TWILIO_AUTH_TOKEN')),
    reason='TWILIO creds not set — live Twilio smoke is opt-in, not CI.')


def test_twilio_credentials_are_live():
    from erp_transport.http_backend import certifi_urlopen
    sid = os.environ['TWILIO_ACCOUNT_SID']
    tok = os.environ['TWILIO_AUTH_TOKEN']
    url = f'https://api.twilio.com/2010-04-01/Accounts/{sid}.json'
    auth = base64.b64encode(f'{sid}:{tok}'.encode()).decode()
    req = urllib.request.Request(url, headers={'Authorization': f'Basic {auth}'})
    with certifi_urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    assert data.get('status') == 'active'      # the account is live
