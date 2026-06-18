"""Tests for utils.rate_limiter — challenge-page detection + proxy retry."""

from unittest.mock import patch, MagicMock

import pytest

from utils import rate_limiter
from utils.rate_limiter import _is_challenge_response, create_session, polite_get


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.text = body
    return r


# ---- _is_challenge_response ----

def test_real_obit_page_is_not_challenge():
    body = (
        '<html><head><title data-react-helmet="true">'
        'Local Hart County, Kentucky Obituaries - Legacy.com</title></head>'
        '<body>' + ("x" * 400_000) + '</body></html>'
    )
    assert _is_challenge_response(_resp(200, body)) is False


def test_cloudflare_just_a_moment_is_challenge():
    body = (
        '<html><head><title>Just a moment...</title></head>'
        '<body>' + ("y" * 5000) + '</body></html>'
    )
    assert _is_challenge_response(_resp(200, body)) is True


def test_cf_challenge_platform_marker_is_challenge():
    body = (
        '<html><head></head><body>'
        '<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1"></script>'
        + ("z" * 5000) + '</body></html>'
    )
    assert _is_challenge_response(_resp(200, body)) is True


def test_large_no_title_body_is_challenge():
    body = "<html><head></head><body>" + ("a" * 60_000) + "</body></html>"
    assert _is_challenge_response(_resp(200, body)) is True


def test_small_body_is_not_challenge():
    body = "<html><body>404</body></html>"
    assert _is_challenge_response(_resp(200, body)) is False


def test_non_200_is_not_challenge():
    body = "<html><body>x</body></html>"
    assert _is_challenge_response(_resp(503, body)) is False


# ---- create_session uses curl_cffi impersonation in PROXY mode ----
# Reproducer for the 2026-06-13 fix: Legacy.com's Cloudflare now fingerprints
# the TLS client, so plain `requests` (even via a residential proxy) gets a hard
# 403. Proxy mode MUST use curl_cffi Chrome impersonation. Before the fix this
# returned a stdlib session (_impersonated False) and the assertion failed.

@patch("utils.rate_limiter._proxy_self_test", lambda *_: None)
@patch("utils.rate_limiter._USE_CURL_CFFI", True)
@patch("utils.rate_limiter.PROXY_URL", "http://u:p@gw:10000")
def test_proxy_mode_uses_curl_cffi_impersonation():
    captured = {}

    def fake_session(impersonate=None):
        captured["impersonate"] = impersonate
        return MagicMock()

    with patch.object(rate_limiter.cffi_requests, "Session", side_effect=fake_session):
        session = create_session()

    # The winning combo is curl_cffi Chrome impersonation routed through the proxy.
    assert captured.get("impersonate") == "chrome120"
    assert getattr(session, "_impersonated", False) is True


# ---- polite_get retries challenge-200 responses ----

@patch("utils.rate_limiter.time.sleep", lambda *_: None)
def test_polite_get_retries_on_challenge_200_then_succeeds():
    challenge = (
        '<html><head><title>Just a moment...</title></head><body>'
        + ("c" * 5000) + '</body></html>'
    )
    success = (
        '<html><head><title data-react-helmet="true">Real Page - Legacy.com</title></head>'
        '<body>' + ("d" * 100_000) + '</body></html>'
    )

    session = MagicMock()
    session.get.side_effect = [_resp(200, challenge), _resp(200, success)]

    resp = polite_get(session, "https://example.com/x", max_retries=3)
    assert resp is not None
    assert resp.status_code == 200
    assert "Real Page" in resp.text
    assert session.get.call_count == 2


@patch("utils.rate_limiter.time.sleep", lambda *_: None)
def test_polite_get_returns_none_when_all_attempts_are_challenges():
    challenge = (
        '<html><head><title>Just a moment...</title></head><body>'
        + ("c" * 5000) + '</body></html>'
    )
    session = MagicMock()
    session.get.return_value = _resp(200, challenge)

    resp = polite_get(session, "https://example.com/x", max_retries=2)
    assert resp is None
    # With PROXY_URL unset, retries == max_retries == 2
    assert session.get.call_count == 2


@patch("utils.rate_limiter.PROXY_URL", "http://u:p@gw:10000")
@patch("utils.rate_limiter.time.sleep", lambda *_: None)
def test_polite_get_in_proxy_mode_does_min_retries():
    challenge = (
        '<html><head><title>Just a moment...</title></head><body>'
        + ("c" * 5000) + '</body></html>'
    )
    session = MagicMock()
    session.get.return_value = _resp(200, challenge)

    # Caller asks for 1 retry; proxy mode bumps to PROXY_MIN_RETRIES.
    resp = polite_get(session, "https://example.com/x", max_retries=1)
    assert resp is None
    assert session.get.call_count == rate_limiter.PROXY_MIN_RETRIES


@patch("utils.rate_limiter.time.sleep", lambda *_: None)
def test_polite_get_returns_real_200_immediately():
    body = (
        '<html><head><title data-react-helmet="true">Hart County Obituaries</title></head>'
        '<body>' + ("d" * 100_000) + '</body></html>'
    )
    session = MagicMock()
    session.get.return_value = _resp(200, body)

    resp = polite_get(session, "https://example.com/x")
    assert resp is not None
    assert session.get.call_count == 1
