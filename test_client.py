"""
test_client.py — Tests for ZendeskTokenManager.

Run with:
    python -m pytest test_client.py -v
    # or
    python test_client.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from unittest.mock import Mock, patch

# Allow running directly from TASK-1/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import (
    ZendeskTokenManager,
    TokenRefreshError,
    TokenNotSetError,
    load_credentials,
)
import first_run

import requests

# ------------------------------------------------------------------ #
#  Helpers                                                            #
# ------------------------------------------------------------------ #

SAMPLE_CREDS = {
    "subdomain": "acme",
    "oauth_token": "initial_token",
    "oauth_refresh_token": "refresh_abc",
    "oauth_client_id": "my_client",
    "oauth_client_secret": "my_secret",
}


def make_mgr(**overrides) -> ZendeskTokenManager:
    """Build a :class:`ZendeskTokenManager` from ``SAMPLE_CREDS`` plus overrides.

    Reference:
        The standard fixture for fully-credentialed manager tests; pass e.g.
        ``ttl=1`` to make a token look expired. Used throughout
        :class:`TestForceRefresh`, :class:`TestCallbacks`,
        :class:`TestErrorScenarios`, and :class:`TestEdgeCases`.
    """
    kwargs = {**SAMPLE_CREDS, **overrides}
    return ZendeskTokenManager(**kwargs)


def mock_200_response(body: dict = None, status_code: int = 200) -> Mock:
    """Fake a successful ``requests.post`` result for the token endpoint.

    Reference:
        Patched in as ``client.requests.post`` return value to exercise the
        success path of :meth:`ZendeskTokenManager._do_refresh` /
        :meth:`_apply_refresh_response` without real network I/O. ``body``
        defaults to a token+refresh pair; override it to simulate rotation,
        a missing ``access_token``, etc.
    """
    r = Mock()
    r.status_code = status_code
    r.json.return_value = body or {"access_token": "new_token", "refresh_token": "new_refresh"}
    r.headers = {}
    return r


def mock_err_response(status_code: int, body: str = "") -> Mock:
    """Fake an error ``requests.post`` result with a status code and text body.

    Reference:
        Used to drive the retry/degradation and permanent-failure branches of
        :meth:`ZendeskTokenManager._do_refresh` (e.g. 503 transient, 401/403/400
        revoked). ``headers`` is empty so :meth:`_parse_retry_after` falls back to
        exponential backoff.
    """
    r = Mock()
    r.status_code = status_code
    r.text = body
    r.headers = {}
    return r


# ------------------------------------------------------------------ #
#  Tests                                                              #
# ------------------------------------------------------------------ #


class TestConstruction:
    """Covers :meth:`ZendeskTokenManager.__init__` and subdomain normalization.

    Reference: exercises ``__init__`` defaults/TTL and the
    :func:`client._normalize_subdomain` cases (bare slug, full URL, port, query).
    """

    def test_minimal(self):
        mgr = ZendeskTokenManager("test")
        assert mgr.subdomain == "test"
        assert mgr._token is None
        assert mgr._refresh_token is None

    def test_with_token(self):
        mgr = ZendeskTokenManager("test", oauth_token="tok")
        assert mgr.get_token() == "tok"
        assert mgr.has_refresh is False

    def test_with_all_creds(self):
        mgr = make_mgr()
        assert mgr.get_token() == "initial_token"
        assert mgr.has_refresh is True
        assert mgr._client_id == "my_client"
        assert mgr._client_secret == "my_secret"

    def test_subdomain_sanitization(self):
        mgr = ZendeskTokenManager("  ACME.ZENDESK.COM  ", oauth_token="tok")
        assert mgr.subdomain == "acme"

    def test_full_url_subdomain_sanitization(self):
        mgr = ZendeskTokenManager(
            " https://ACME.ZENDESK.COM/admin/people ",
            oauth_token="tok",
        )
        assert mgr.subdomain == "acme"

    def test_hostname_with_port_sanitization(self):
        mgr = ZendeskTokenManager(
            "https://acme.zendesk.com:443/admin",
            oauth_token="tok",
        )
        assert mgr.subdomain == "acme"

    def test_hostname_with_query_sanitization(self):
        mgr = ZendeskTokenManager(
            "acme.zendesk.com?ticket=1",
            oauth_token="tok",
        )
        assert mgr.subdomain == "acme"

    def test_default_ttl(self):
        mgr = ZendeskTokenManager("test")
        assert mgr._ttl == ZendeskTokenManager.DEFAULT_TTL

    def test_custom_ttl(self):
        mgr = ZendeskTokenManager("test", ttl=7200)
        assert mgr._ttl == 7200


class TestGetToken:
    """Covers :meth:`ZendeskTokenManager.get_token`.

    Reference: the proactive-refresh decision (``_age_pct`` vs
    ``REFRESH_THRESHOLD``), the ``TokenNotSetError`` guard, and graceful
    degradation when a refresh fails or no client creds are configured.
    """

    def test_raises_when_no_token(self):
        mgr = ZendeskTokenManager("test")
        with pytest_raises(TokenNotSetError, "No OAuth token"):
            mgr.get_token()

    def test_returns_initial_token(self):
        mgr = ZendeskTokenManager("test", oauth_token="hello")
        assert mgr.get_token() == "hello"

    def test_proactive_refresh_not_triggered_when_fresh(self):
        mgr = make_mgr(ttl=3600)
        assert mgr.get_token() == "initial_token"

    def test_proactive_refresh_triggered_when_expired(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0  # force past threshold

        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response()
            token = mgr.get_token()
            assert token == "new_token"

    def test_proactive_refresh_degrades_on_failure(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0

        with patch("client.requests.post") as mock_post:
            with patch("client.time.sleep", return_value=None):
                mock_post.return_value = mock_err_response(503)
                token = mgr.get_token()
                assert token == "initial_token"

    def test_proactive_refresh_degrades_on_revoked(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0

        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_err_response(401, "invalid_grant")
            token = mgr.get_token()
            assert token == "initial_token"

    def test_does_not_refresh_without_creds(self):
        mgr = ZendeskTokenManager("test", oauth_token="tok", ttl=1)
        mgr._acquired_at = 0
        assert mgr.get_token() == "tok"


class TestSetToken:
    """Covers :meth:`ZendeskTokenManager.set_token` (in-memory update + validation).

    Reference: verifies token/refresh replacement and the non-empty-string guard.
    """

    def test_updates_token(self):
        mgr = ZendeskTokenManager("test")
        mgr.set_token("new_tok")
        assert mgr.get_token() == "new_tok"
        assert mgr.has_refresh is False

    def test_updates_token_and_refresh(self):
        mgr = ZendeskTokenManager("test")
        mgr.set_token("new_tok", "new_ref")
        assert mgr.get_token() == "new_tok"
        assert mgr.has_refresh is True

    def test_rejects_empty_string(self):
        mgr = ZendeskTokenManager("test")
        try:
            mgr.set_token("", "x")
            assert False, "should have raised"
        except ValueError:
            pass


class TestForceRefresh:
    """Covers :meth:`ZendeskTokenManager.force_refresh` (the reactive 401 path).

    Reference: success, revocation (raises ``TokenRefreshError``), degradation
    without a refresh token, the ``TokenNotSetError`` / missing-creds guards, and
    the exact request payload sent to ``client.requests.post``.
    """

    def test_refreshes_successfully(self):
        mgr = make_mgr()
        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response()
            token = mgr.force_refresh()
            assert token == "new_token"

    def test_force_refresh_returns_fresh_token_even_if_callback_mutates_state(self):
        mgr = make_mgr()
        mgr.on_refresh(lambda t, rt: mgr.set_token("override", rt))

        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response({"access_token": "fresh_token"})
            token = mgr.force_refresh()

        assert token == "fresh_token"
        assert mgr.get_token() == "override"

    def test_raises_on_revoked(self):
        mgr = make_mgr()
        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_err_response(401, "invalid_grant")
            try:
                mgr.force_refresh()
                assert False, "should have raised"
            except TokenRefreshError:
                pass

    def test_degrades_without_refresh_token(self):
        mgr = ZendeskTokenManager("test", oauth_token="tok")
        assert mgr.force_refresh() == "tok"

    def test_raises_without_any_token(self):
        mgr = ZendeskTokenManager("test")
        try:
            mgr.force_refresh()
            assert False, "should have raised"
        except TokenNotSetError:
            pass

    def test_raises_without_client_creds(self):
        mgr = ZendeskTokenManager("test", oauth_token="tok", oauth_refresh_token="ref")
        try:
            mgr.force_refresh()
            assert False, "should have raised"
        except TokenRefreshError:
            pass

    def test_sends_correct_payload(self):
        mgr = make_mgr()
        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response()
            mgr.force_refresh()
            call_kwargs = mock_post.call_args.kwargs
            assert call_kwargs["json"]["grant_type"] == "refresh_token"
            assert call_kwargs["json"]["refresh_token"] == "refresh_abc"
            assert call_kwargs["json"]["client_id"] == "my_client"
            assert call_kwargs["json"]["client_secret"] == "my_secret"


class TestProperties:
    """Covers the read-only properties :attr:`has_refresh` and :attr:`age`."""

    def test_has_refresh_true(self):
        assert make_mgr().has_refresh is True

    def test_has_refresh_false(self):
        assert ZendeskTokenManager("test", oauth_token="tok").has_refresh is False

    def test_age_increases(self):
        mgr = ZendeskTokenManager("test", oauth_token="tok")
        a1 = mgr.age
        time.sleep(0.01)
        assert mgr.age > a1


class TestCallbacks:
    """Covers :meth:`on_refresh` registration and :meth:`_notify_all` dispatch.

    Reference: callbacks fire on refresh with the rotated (or ``None``) refresh
    token, multiple callbacks run in order, a throwing callback is isolated, and
    no callback fires when the refresh itself failed.
    """

    def test_callback_fired_on_refresh(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0
        results = []

        mgr.on_refresh(lambda t, rt: results.append((t, rt)))
        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response({
                "access_token": "cb_tok", "refresh_token": "cb_ref"
            })
            mgr.get_token()

        assert results == [("cb_tok", "cb_ref")]

    def test_callback_receives_none_when_refresh_not_rotated(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0
        results = []

        mgr.on_refresh(lambda t, rt: results.append((t, rt)))
        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response({"access_token": "cb_tok"})
            mgr.get_token()

        assert results == [("cb_tok", None)]

    def test_multiple_callbacks(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0
        results = []

        mgr.on_refresh(lambda t, rt: results.append("A"))
        mgr.on_refresh(lambda t, rt: results.append("B"))
        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response()
            mgr.get_token()

        assert results == ["A", "B"]

    def test_failing_callback_does_not_block(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0
        results = []

        mgr.on_refresh(lambda t, rt: (_ for _ in ()).throw(ValueError("oops")))
        mgr.on_refresh(lambda t, rt: results.append("survived"))
        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response()
            mgr.get_token()

        assert results == ["survived"]

    def test_no_callback_on_failed_refresh(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0
        results = []

        mgr.on_refresh(lambda t, rt: results.append("should_not_happen"))
        with patch("client.requests.post") as mock_post:
            with patch("client.time.sleep", return_value=None):
                mock_post.return_value = mock_err_response(503)
                mgr.get_token()

        assert results == []


class TestLoadCredentials:
    """Covers :func:`client.load_credentials` (JSON file -> configured manager).

    Reference: valid file with/without refresh token, bad/future ``acquired_at``
    handling, and the error cases (missing file, missing required field, invalid
    JSON) that propagate as ``FileNotFoundError`` / ``KeyError`` /
    ``json.JSONDecodeError``.
    """

    def test_loads_valid_file(self):
        data = {
            "subdomain": "testsub",
            "oauth_token": "tok123",
            "oauth_refresh_token": "ref456",
            "oauth_client_id": "cid",
            "oauth_client_secret": "csec",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name

        mgr = load_credentials(path)
        assert mgr.get_token() == "tok123"
        assert mgr.has_refresh is True
        os.unlink(path)

    def test_loads_without_refresh(self):
        data = {
            "subdomain": "s",
            "oauth_token": "t",
            "oauth_client_id": "c",
            "oauth_client_secret": "s",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name

        mgr = load_credentials(path)
        assert mgr.get_token() == "t"
        assert mgr.has_refresh is False
        os.unlink(path)

    def test_invalid_acquired_at_type(self):
        data = {
            "subdomain": "testsub",
            "oauth_token": "tok123",
            "oauth_client_id": "cid",
            "oauth_client_secret": "csec",
            "acquired_at": "not-a-timestamp",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name

        try:
            load_credentials(path)
            assert False, "should have raised"
        except ValueError:
            pass
        os.unlink(path)

    def test_future_acquired_at_clamped(self):
        data = {
            "subdomain": "testsub",
            "oauth_token": "tok123",
            "oauth_client_id": "cid",
            "oauth_client_secret": "csec",
            "acquired_at": time.time() + 3600,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name

        mgr = load_credentials(path)
        assert mgr.age >= 0
        assert mgr._age_pct() >= 0
        os.unlink(path)

    def test_file_not_found(self):
        try:
            load_credentials("/nonexistent/path.json")
            assert False, "should have raised"
        except FileNotFoundError:
            pass

    def test_missing_required_field(self):
        data = {"subdomain": "s", "oauth_token": "t"}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = f.name

        try:
            load_credentials(path)
            assert False, "should have raised"
        except KeyError:
            pass
        os.unlink(path)

    def test_invalid_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json")
            path = f.name

        try:
            load_credentials(path)
            assert False, "should have raised"
        except json.JSONDecodeError:
            pass
        os.unlink(path)


class TestThreadSafety:
    """Covers the double-checked locking in :meth:`get_token` / :meth:`_refresh_if_needed`.

    Reference: concurrent reads are consistent, N threads crossing the threshold
    together cause exactly one ``requests.post``, and a callback that re-enters
    the manager (:meth:`set_token`) does not deadlock — because :meth:`_notify_all`
    runs outside the lock.
    """

    def test_concurrent_get_token(self):
        mgr = ZendeskTokenManager("test", oauth_token="tok")
        results = []

        def read():
            results.append(mgr.get_token())

        threads = [threading.Thread(target=read) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results == ["tok"] * 10

    def test_concurrent_refresh_single_call(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0
        call_count = []

        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response()

            def read():
                mgr.get_token()
                call_count.append(1)

            threads = [threading.Thread(target=read) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(call_count) == 5
        assert mock_post.call_count == 1

    def test_callback_can_reenter_manager(self):
        mgr = make_mgr()
        mgr.on_refresh(lambda t, rt: mgr.set_token(t, rt))

        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response()

            thread = threading.Thread(target=mgr.force_refresh)
            thread.start()
            thread.join(timeout=0.5)

        assert not thread.is_alive()
        assert mgr.get_token() == "new_token"


class TestErrorScenarios:
    """Covers the retry/error matrix in :meth:`ZendeskTokenManager._do_refresh`.

    Reference: exhausted retries on 503, timeout and connection-error retries,
    429 rate-limit-then-success (via :meth:`_parse_retry_after`), permanent
    400/403, non-JSON body, and a 200 missing ``access_token``. ``client.time.sleep``
    is patched out so backoff runs instantly.
    """

    def test_max_retries_exhausted(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0

        with patch("client.requests.post") as mock_post:
            with patch("client.time.sleep", return_value=None):
                mock_post.return_value = mock_err_response(503)
                token = mgr.get_token()
                assert token == "initial_token"
                assert mock_post.call_count == ZendeskTokenManager.MAX_RETRIES

    def test_timeout_retry(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0

        with patch("client.requests.post") as mock_post:
            with patch("client.time.sleep", return_value=None):
                mock_post.side_effect = requests.exceptions.Timeout("timeout")
                token = mgr.get_token()
                assert token == "initial_token"
                assert mock_post.call_count == ZendeskTokenManager.MAX_RETRIES

    def test_connection_error_retry(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0

        with patch("client.requests.post") as mock_post:
            with patch("client.time.sleep", return_value=None):
                mock_post.side_effect = requests.exceptions.ConnectionError("refused")
                token = mgr.get_token()
                assert token == "initial_token"

    def test_rate_limited_then_succeeds(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0
        attempts = []

        with patch("client.requests.post") as mock_post:
            with patch("client.time.sleep", return_value=None):
                def side_effect(*a, **kw):
                    attempts.append(1)
                    if len(attempts) == 1:
                        r = Mock()
                        r.status_code = 429
                        r.headers = {"Retry-After": "0"}
                        return r
                    return mock_200_response({"access_token": "after_429"})

                mock_post.side_effect = side_effect
                token = mgr.get_token()
                assert token == "after_429"

    def test_400_error_raises_on_force_refresh(self):
        mgr = make_mgr()
        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_err_response(400, "bad request")
            try:
                mgr.force_refresh()
                assert False, "should have raised"
            except TokenRefreshError:
                pass

    def test_403_error_raises_on_force_refresh(self):
        mgr = make_mgr()
        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_err_response(403, "forbidden")
            try:
                mgr.force_refresh()
                assert False, "should have raised"
            except TokenRefreshError:
                pass

    def test_non_json_response(self):
        mgr = make_mgr()
        with patch("client.requests.post") as mock_post:
            r = Mock()
            r.status_code = 200
            r.json.side_effect = ValueError("not json")
            r.headers = {}
            mock_post.return_value = r
            try:
                mgr.force_refresh()
                assert False, "should have raised"
            except TokenRefreshError:
                pass

    def test_missing_access_token_in_response(self):
        mgr = make_mgr()
        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response({"refresh_token": "r"})
            try:
                mgr.force_refresh()
                assert False, "should have raised"
            except TokenRefreshError:
                pass


class TestEdgeCases:
    """Covers refresh-token rotation and clock/subdomain bookkeeping.

    Reference: :meth:`_apply_refresh_response` storing (or preserving) the
    refresh token, ``_acquired_at`` resetting on refresh, and the subdomain
    staying normalized after construction.
    """

    def test_refresh_token_rotation(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0

        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response({
                "access_token": "new_tok",
                "refresh_token": "rotated_refresh",
            })
            mgr.get_token()
            assert mgr._refresh_token == "rotated_refresh"

    def test_refresh_token_not_rotated(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0

        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response({
                "access_token": "new_tok",
                # no refresh_token in response
            })
            mgr.get_token()
            assert mgr._refresh_token == "refresh_abc"

    def test_acquired_at_reset_on_refresh(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0

        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response()
            mgr.get_token()
            assert mgr._acquired_at > 0

    def test_subdomain_preserved_after_constructor(self):
        mgr = ZendeskTokenManager("  MY-SUB  ", oauth_token="tok")
        assert mgr.subdomain == "my-sub"


class TestInfo:
    """Display test — print on success.

    Reference: a human-readable smoke test of the end-to-end refresh-once-then-
    cache behavior of :meth:`get_token`; asserts a single ``requests.post`` and
    prints the resulting manager state.
    """

    def test_info(self):
        mgr = make_mgr(ttl=1)
        mgr._acquired_at = 0
        with patch("client.requests.post") as mock_post:
            mock_post.return_value = mock_200_response({
                "access_token": "refreshed_tok",
                "refresh_token": "refreshed_ref",
            })
            t = mgr.get_token()
            sys.stdout.write(f"\n  Initial call → {t}\n")
            t2 = mgr.get_token()
            sys.stdout.write(f"  Second call  → {t2} (no extra refresh)\n")
            sys.stdout.write(f"  has_refresh  → {mgr.has_refresh}\n")
            sys.stdout.write(f"  age          → {mgr.age:.3f}s\n")
            sys.stdout.write(f"  callbacks    → {len(mgr._callbacks)}\n")
            sys.stdout.write(f"  refresh_tok  → {mgr._refresh_token}\n")
            assert t == "refreshed_tok"
            assert t2 == "refreshed_tok"
            assert mgr._refresh_token == "refreshed_ref"
            assert mock_post.call_count == 1


class TestFirstRunHelpers:
    """Covers the pure helpers in :mod:`first_run` (no real network/browser).

    Reference: :func:`first_run.sanitize_subdomain`, :func:`first_run.build_auth_url`,
    :func:`first_run.parse_redirect_url` (state validation -> ``SystemExit``), and
    the request-failure exits of :func:`first_run.exchange_code` /
    :func:`first_run.verify_token`.
    """

    def test_sanitize_subdomain_accepts_full_hostname(self):
        assert first_run.sanitize_subdomain("https://ACME.zendesk.com/admin") == "acme"

    def test_build_auth_url_includes_state(self):
        url = first_run.build_auth_url("acme", "client-id", "read write", "state123")
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        assert params["state"] == ["state123"]
        assert params["client_id"] == ["client-id"]
        assert params["scope"] == ["read write"]

    def test_parse_redirect_url_validates_state(self):
        url = "http://localhost/callback?code=abc123&state=expected"
        assert first_run.parse_redirect_url(url, "expected") == "abc123"

        with pytest_raises(SystemExit):
            first_run.parse_redirect_url(url, "wrong")

    def test_exchange_code_handles_request_exception(self):
        with patch("first_run.requests.post", side_effect=requests.Timeout("boom")):
            with pytest_raises(SystemExit):
                first_run.exchange_code("acme", "cid", "secret", "code", "read")

    def test_verify_token_handles_request_exception(self):
        with patch("first_run.requests.get", side_effect=requests.ConnectionError("down")):
            with pytest_raises(SystemExit):
                first_run.verify_token("acme", "token")


# ------------------------------------------------------------------ #
#  pytest compatibility (works without pytest installed)              #
# ------------------------------------------------------------------ #


def pytest_raises(exc_cls, msg=None):
    """Context-manager for exception assertions (pytest-like API).

    A dependency-free stand-in for ``pytest.raises`` so this suite runs both
    under pytest and via the bundled :func:`main`. Asserts the block raises
    ``exc_cls`` (and, if ``msg`` is given, that it appears in the message).

    Reference:
        Used by :class:`TestGetToken` and :class:`TestFirstRunHelpers`. Other
        test classes use plain ``try/except`` for the same purpose.

    Args:
        exc_cls: The exception type expected to be raised.
        msg: Optional substring required in the exception message.
    """
    import contextlib

    @contextlib.contextmanager
    def _raises(cls, message=None):
        try:
            yield
        except cls as e:
            if message and message not in str(e):
                raise AssertionError(
                    f"Expected message {message!r} not in {e!r}"
                )
        except Exception as e:
            raise AssertionError(f"Expected {cls.__name__}, got {type(e).__name__}: {e}")
        else:
            raise AssertionError(f"Expected {cls.__name__} but no exception was raised")

    return _raises(exc_cls, msg)


# ------------------------------------------------------------------ #
#  Main (for running without pytest)                                  #
# ------------------------------------------------------------------ #

def main():
    """Run every ``Test*`` class without pytest and print a pass/fail summary.

    Reflectively instantiates each class in the ``classes`` list, runs its
    ``test_*`` methods in sorted order, prints a tick/cross per test plus a
    traceback on failure, and returns an exit code (0 = all passed).

    Reference:
        The ``__main__`` entry point for ``python test_client.py``. The
        equivalent pytest invocation is ``python -m pytest test_client.py -v``.

    Returns:
        ``0`` if all tests passed, ``1`` otherwise (suitable for ``sys.exit``).
    """
    print("=" * 56)
    print("  ZendeskTokenManager — Test Suite")
    print("=" * 56)

    classes = [
        TestConstruction,
        TestGetToken,
        TestSetToken,
        TestForceRefresh,
        TestProperties,
        TestCallbacks,
        TestLoadCredentials,
        TestThreadSafety,
        TestErrorScenarios,
        TestEdgeCases,
        TestInfo,
        TestFirstRunHelpers,
    ]

    total_ran = 0
    total_failed = 0
    for cls in classes:
        name = cls.__name__.replace("Test", "")
        print(f"\n  [{name}]")
        obj = cls()
        ran = 0
        failed = 0
        for attr in sorted(dir(cls)):
            if attr.startswith("test_"):
                ran += 1
                try:
                    getattr(obj, attr)()
                    print(f"  ✓ {attr}")
                except Exception as e:
                    failed += 1
                    import traceback
                    traceback.print_exc()
                    print(f"  ✗ {attr}: {e}")
        total_ran += ran
        total_failed += failed

    print(f"\n{'=' * 56}")
    if total_failed == 0:
        print(f"  ✅  All {total_ran} tests passed")
    else:
        print(f"  ❌  {total_failed}/{total_ran} tests failed")
    print(f"{'=' * 56}")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
