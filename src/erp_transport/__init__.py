"""Production wire transports for the ERP harness (P3).

The harness is import-pure — nothing in `erp_harness` imports a network stack;
the `Backend` protocol is its only wire boundary (test_harness_purity). The
real HTTPS client therefore lives HERE, outside the harness, and is injected
into the SafetyEnforcer at runtime. CI exercises the harness against the
in-process twin; this package's HTTP client is exercised by mocked-transport
unit tests (injectable urlopen) and a credential-gated live smoke.
"""
from erp_transport.http_backend import (
    HttpBackend, OAuthClientCredentials, OAuthError,
)
from erp_transport.sql_backend import SqlBackend
from erp_transport.web_fetch import playwright_fetcher, static_fetcher
from erp_transport.live import WallClock, build_live_backend, build_live_enforcer

__all__ = ['HttpBackend', 'OAuthClientCredentials', 'OAuthError', 'SqlBackend',
           'static_fetcher', 'playwright_fetcher', 'WallClock',
           'build_live_backend', 'build_live_enforcer']
