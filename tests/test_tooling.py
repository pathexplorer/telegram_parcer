"""
Tests for operator tooling (FR-OPS-3/4/5).

Covers:
  - ``emergency/reset_cursors.py`` pure helpers (gap/status formatting,
    legacy-vs-typed cursor extraction, dialog-cache lookup, titles)
  - shell syntax lint (``bash -n``) + shebang audit for all project scripts
    (run_local.sh, deploy.sh, scripts/*.sh) so CI catches regressions
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SH_SCRIPTS = sorted(
    str(p.relative_to(PROJECT_ROOT))
    for p in [*PROJECT_ROOT.glob("*.sh"), *PROJECT_ROOT.glob("scripts/*.sh")]
)


@pytest.fixture(scope="module")
def reset_cursors():
    """Import emergency.reset_cursors with its import-time side effects mocked."""
    for mod in list(sys.modules):
        if mod.startswith("emergency"):
            del sys.modules[mod]
    with patch(
        "gcp_actions.common_utils.handle_logs.run_handle_logs", return_value=None
    ):
        import emergency.reset_cursors as rc

    return rc


# ============================================================================
# emergency/reset_cursors.py — FR-OPS-3
# ============================================================================

class TestResetCursorsHelpers:
    """Pure helpers of the emergency cursor-reset utility."""

    def test_fmt_gap_empty_chat(self, reset_cursors):
        assert "empty chat" in reset_cursors._fmt_gap(0, 0)

    def test_fmt_gap_new_chat(self, reset_cursors):
        assert "NEW" in reset_cursors._fmt_gap(0, 50)

    def test_fmt_gap_behind(self, reset_cursors):
        assert reset_cursors._fmt_gap(10, 25) == "📩 +15 new"

    def test_fmt_gap_stale(self, reset_cursors):
        assert "STALE" in reset_cursors._fmt_gap(30, 10)
        assert "cursor ahead by 20" in reset_cursors._fmt_gap(30, 10)

    def test_fmt_gap_up_to_date(self, reset_cursors):
        assert reset_cursors._fmt_gap(10, 10) == "✅ up-to-date"

    def test_extract_cursor_typed_dict(self, reset_cursors):
        assert reset_cursors._extract_cursor({"last_processed_id": 42}) == 42

    def test_extract_cursor_legacy_list(self, reset_cursors):
        assert reset_cursors._extract_cursor(["@chat", 42]) == 42

    def test_extract_cursor_corrupt(self, reset_cursors):
        assert reset_cursors._extract_cursor("garbage") == 0
        assert reset_cursors._extract_cursor({"ref": "@chat"}) == 0

    def test_extract_ref_typed_and_legacy(self, reset_cursors):
        assert reset_cursors._extract_ref({"ref": "@chat"}) == "@chat"
        assert reset_cursors._extract_ref(["@chat", 42]) == "@chat"
        assert reset_cursors._extract_ref("corrupt") == ""

    def test_lookup_dialog_public_id(self, reset_cursors):
        dlg = MagicMock()
        cache = {1511100059: dlg}
        assert reset_cursors._lookup_dialog("1511100059", cache) is dlg

    def test_lookup_dialog_internal_supergroup_id(self, reset_cursors):
        """Supergroups are cached under -100-prefixed internal IDs."""
        dlg = MagicMock()
        cache = {-1001511100059: dlg}
        assert reset_cursors._lookup_dialog("1511100059", cache) is dlg

    def test_lookup_dialog_missing(self, reset_cursors):
        assert reset_cursors._lookup_dialog("999999", {}) is None

    def test_safe_title_priority_chain(self, reset_cursors):
        channel = MagicMock()
        channel.title = "My Channel"
        user = MagicMock()
        user.first_name = "John"
        user.last_name = "Doe"
        del user.title
        bare = MagicMock(spec=[])
        assert reset_cursors._safe_title(channel) == "My Channel"
        assert reset_cursors._safe_title(user) == "John Doe"
        assert reset_cursors._safe_title(bare) == "Unknown"

    def test_build_dialog_cache_iterates_dialogs(self, reset_cursors):
        dlg = MagicMock()
        dlg.id = 42

        async def _dialogs():
            yield dlg

        client = MagicMock()
        client.iter_dialogs.return_value = _dialogs()
        import asyncio

        cache = asyncio.run(reset_cursors._build_dialog_cache(client))
        assert cache == {42: dlg}

    def test_get_latest_message_id(self, reset_cursors):
        import asyncio
        from unittest.mock import AsyncMock

        msg = MagicMock()
        msg.id = 777
        client = MagicMock()
        client.get_input_entity = AsyncMock(return_value=MagicMock())
        client.get_messages = AsyncMock(return_value=[msg])
        assert asyncio.run(reset_cursors._get_latest_message_id(client, MagicMock())) == 777

    def test_get_latest_message_id_empty_chat_is_zero(self, reset_cursors):
        import asyncio
        from unittest.mock import AsyncMock

        client = MagicMock()
        client.get_input_entity = AsyncMock(return_value=MagicMock())
        client.get_messages = AsyncMock(return_value=[])
        assert asyncio.run(reset_cursors._get_latest_message_id(client, MagicMock())) == 0

    def test_get_latest_message_id_error_is_minus_one(self, reset_cursors):
        import asyncio
        from unittest.mock import AsyncMock

        client = MagicMock()
        client.get_input_entity = AsyncMock(return_value=MagicMock())
        client.get_messages = AsyncMock(side_effect=RuntimeError("flood"))
        assert asyncio.run(reset_cursors._get_latest_message_id(client, MagicMock())) == -1


# ============================================================================
# Shell scripts — FR-OPS-4/5
# ============================================================================

class TestShellScripts:
    """run_local.sh / deploy.sh / scripts/*.sh must stay runnable."""

    @pytest.mark.parametrize("script", SH_SCRIPTS)
    def test_bash_syntax_valid(self, script):
        """`bash -n` lints the script without executing it."""
        result = subprocess.run(
            ["bash", "-n", str(PROJECT_ROOT / script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed for {script}:\n{result.stderr}"

    @pytest.mark.parametrize("script", SH_SCRIPTS)
    def test_shebang_present(self, script):
        """Every entry-point script must declare an interpreter."""
        first_line = (PROJECT_ROOT / script).read_text().splitlines()[0]
        assert first_line.startswith("#!"), f"{script} is missing a shebang"