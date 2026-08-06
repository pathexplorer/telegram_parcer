"""
GCF Deployment Simulation Tests
================================
These tests imitate the Google Cloud Functions runtime without deploying.

They verify that:
  - The entry-point function ``main(request)`` accepts a Flask request
  - All GCP dependencies (Secret Manager, Firestore, Telethon) are wired correctly
  - Error paths return appropriate responses (missing config, empty keywords, etc.)
  - The function returns the HTTP status and body that Cloud Functions expects

Usage:
    cd telegram_parcer
    uv run pytest tests/test_gcf_deploy.py -v

    # With coverage:
    uv run pytest tests/test_gcf_deploy.py -v --cov=telegram_parcer --cov=telegram
"""

from __future__ import annotations

import os
import sys
import logging
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helper — build a minimal mock request (like GCF sends)
# ---------------------------------------------------------------------------

class _MockRequest:
    """A lightweight stand-in for the Flask Request that GCF wraps around the
    incoming HTTP call.  We only need it to be a non-None object; the main()
    handler does not inspect the request body.

    Includes a dummy Authorization header so the new auth gate passes.
    """

    method = "GET"
    headers: dict[str, str] = {"Authorization": "Bearer mock-oidc-token"}
    environ: dict[str, str] = {}

    def get_json(self, silent: bool = False) -> dict | None:
        return None


def _make_request(
    method: str = "GET",
    json_body: dict | None = None,
    headers: dict | None = None,
) -> _MockRequest:
    """Create a mock request object similar to what GCF provides."""
    req = _MockRequest()
    req.method = method
    if headers:
        req.headers = headers
    if json_body is not None:
        req.get_json = MagicMock(return_value=json_body)
    return req


# ===================================================================
# Test 1: Happy path — full end-to-end GCF invocation
# ===================================================================

@pytest.mark.usefixtures("mock_all_gcp_deps")
class TestGCFDeployHappyPath:
    """Happy-path: all dependencies resolve, config is valid, polling succeeds."""

    def test_main_returns_200_and_polling_complete(self):
        """Simulate a healthy GCF invocation — should return 'Polling complete', 200."""
        # Import AFTER mocks are in place (import-time code runs with mocks)
        from telegram_parcer.main import main

        request = _make_request()
        body, status = main(request)

        assert status == 200, f"Expected 200, got {status}"
        assert body == "Polling complete", f"Unexpected body: {body!r}"

    def test_main_accepts_none_request(self):
        """main(request=None) should work (defensive coding pattern)."""
        from telegram_parcer.main import main

        body, status = main(None)

        assert status == 200
        assert body == "Polling complete"


# ===================================================================
# Test 2: Configuration loading
# ===================================================================

@pytest.mark.usefixtures("mock_all_gcp_deps")
class TestConfigLoading:
    """Verify that Secrets and Firestore config are loaded correctly."""

    def test_secrets_injected_into_environment(self):
        """After main() is called, secret env vars (API_ID, API_HASH) are populated.

        Secrets are injected lazily on the first main() invocation, not at
        import time (so warm instances can refresh Firestore config without
        re-fetching secrets).
        """
        from telegram_parcer.main import main

        # Trigger secret injection by invoking main()
        body, status = main(_make_request())
        assert status == 200

        # Now secrets should be in the environment
        assert os.environ.get("API_ID") == "12345"
        assert os.environ.get("API_HASH") == "abc123hash"

    def test_firestore_keywords_and_chats_loaded(self):
        """starter_conf.forming_configuration() reads from Firestore mock."""
        from telegram_parcer.telegram.starter_conf import forming_configuration
        from unittest.mock import patch

        # forming_configuration is called at import time too,
        # we re-call it here to verify behaviour directly.
        kw_list, chat_list, cursors, _known = forming_configuration()

        assert isinstance(kw_list, list)
        assert isinstance(chat_list, list)
        assert len(kw_list) >= 1, "Keywords list is empty!"
        assert len(chat_list) >= 1, "Chats list is empty!"


# ===================================================================
# Test 3: Error paths — missing / invalid configuration
# ===================================================================

class TestGCFDeployErrors:
    """Error scenarios that should be surfaced before or during GCF invocation."""

    def test_missing_required_env_var_returns_500(self):
        """If API_ID is missing after config load, main() returns 500 (not crash)."""
        from unittest.mock import patch

        env_without_secrets = {
            "GCP_PROJECT_ID": "test-project-123",
            "K_SERVICE": "telegram-parcer",
            "TELEGRAM_SECRETS": "telegram-secrets",
            "NOTIFICATION_CHAT": "-1001234567890",
            # API_ID and API_HASH are deliberately OMITTED from the mock secret
        }

        # Override the secret manager to return an empty dict (no secrets)
        sm_mock = MagicMock()
        sm_mock.get_secret_json.return_value = {}

        fs_mock = MagicMock()
        fs_mock.load_firejson.side_effect = [
            {"word": ["kw"], "chats": ["@test"]},
            {"123": {"ref": "@test", "last_processed_id": 0, "alerted_keys": "", "schema_version": 1}},
        ]
        fs_mock.unpack_array_to_csv_string.side_effect = (
            lambda doc, field: ",".join(str(x) for x in doc.get(field, []))
        )

        # Clean cached modules and the per-process config cache
        for mod in list(sys.modules):
            if mod.startswith("telegram_parcer") or mod.startswith("telegram"):
                del sys.modules[mod]

        with patch.dict(os.environ, env_without_secrets, clear=False):
            with patch("gcp_actions.common_utils.handle_logs.run_handle_logs"):
                with patch(
                    "gcp_actions.common_utils.init_config.SecretManagerClient",
                    return_value=sm_mock,
                ):
                    with patch(
                        "gcp_actions.firestore_box.json_manipulations.FirestoreMagic",
                        return_value=fs_mock,
                    ):
                        with patch("telethon.TelegramClient"):
                            import importlib
                            import telegram_parcer.main

                            importlib.reload(telegram_parcer.main)
                            # Clear the per-process config cache so secrets
                            # are re-injected on the next main() call.
                            telegram_parcer.main._config_cache = None
                            telegram_parcer.main._config_loaded_at = 0.0

                            # main() gracefully returns 500 when secrets are missing
                            req = _make_request()
                            body, status = telegram_parcer.main.main(req)

                            assert status == 500, f"Expected 500, got {status}"
                            assert "Configuration error" in body

    def test_empty_keywords_list_causes_runtime_error(self):
        """If Firestore returns empty keywords, forming_configuration should raise."""
        from unittest.mock import patch

        # Simulate an empty keywords document
        fs = MagicMock()
        fs.load_firejson.return_value = {"word": [], "chats": []}  # empty!
        fs.unpack_array_to_csv_string.return_value = ""

        with patch(
            "gcp_actions.firestore_box.json_manipulations.FirestoreMagic",
            return_value=fs,
        ):
            # Directly call the function under test (it creates its own
            # FirestoreMagic instances internally)
            from telegram_parcer.telegram.starter_conf import forming_configuration

            with pytest.raises(RuntimeError, match="Keywords list is empty"):
                forming_configuration()


# ===================================================================
# Test 4: GCF-specific behaviours
# ===================================================================

@pytest.mark.usefixtures("mock_all_gcp_deps")
class TestGCFSpecific:
    """Tests that verify GCF-specific contract."""

    def test_response_is_tuple_of_body_and_status(self):
        """GCF expects (str, int) tuple from the handler."""
        from telegram_parcer.main import main

        result = main(_make_request())

        assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
        assert len(result) == 2, f"Expected 2 elements, got {len(result)}"
        assert isinstance(result[0], str), f"Body should be str, got {type(result[0])}"
        assert isinstance(result[1], int), f"Status should be int, got {type(result[1])}"

    def test_post_request_same_as_get(self):
        """Both GET and POST should work identically (function is request-agnostic)."""
        from telegram_parcer.main import main

        get_body, get_status = main(_make_request(method="GET"))
        post_body, post_status = main(_make_request(method="POST"))

        assert get_status == post_status == 200
        assert get_body == post_body


# ===================================================================
# Test 5: Environment detection (cloud vs local)
# ===================================================================

class TestEnvironmentDetection:
    """Verify cloud/local detection is correct."""

    def test_detects_cloud_run_environment(self):
        """When K_SERVICE is set, check_cloud_or_local_run returns service name."""
        from gcp_actions.common_utils.local_runner import check_cloud_or_local_run

        with patch.dict(os.environ, {"K_SERVICE": "my-service", "GCP_PROJECT_ID": "proj"}):
            # Clear the lru_cache on the function
            check_cloud_or_local_run.cache_clear()
            result = check_cloud_or_local_run()
            assert result == "my-service"

    def test_detects_local_environment(self):
        """When K_SERVICE is absent but GCP_PROJECT_ID is set, local mode is detected."""
        from gcp_actions.common_utils.local_runner import check_cloud_or_local_run

        # Clear lru_cache so previous test runs don't pollute this one
        check_cloud_or_local_run.cache_clear()

        # Simulate a local run: no K_SERVICE / K_REVISION, but GCP_PROJECT_ID is present
        with patch.dict(
            os.environ,
            {"K_SERVICE": "", "K_REVISION": "", "GCP_PROJECT_ID": "test-project-123"},
            clear=False,
        ):
            check_cloud_or_local_run.cache_clear()
            result = check_cloud_or_local_run()
            # In local mode it returns DEFAULT_APP_ID = 'local-dev-mode'
            assert "local" in result.lower()
            assert result == "local-dev-mode"
