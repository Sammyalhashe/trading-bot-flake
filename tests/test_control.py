import asyncio
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.control import ControlCenter, TelegramPoller, serve_unix  # noqa: E402

CHAT_ID = 4242


@pytest.fixture
def cfg():
    return SimpleNamespace(bear_position_scale=0.25, max_concurrent_positions=5, stopout_cooldown_hours=12.0)


@pytest.fixture
def control_path(tmp_path):
    return tmp_path / "trading_state.control.json"


class TestControlCenter:
    def test_pause_survives_restart(self, control_path, cfg):
        center = ControlCenter(control_path, cfg)
        assert not center.is_paused()
        center.handle("pause strategy under review")
        assert ControlCenter(control_path, cfg).is_paused()

    def test_resume_clears_pause(self, control_path, cfg):
        center = ControlCenter(control_path, cfg)
        center.handle("pause")
        center.handle("resume")
        assert not ControlCenter(control_path, cfg).is_paused()

    def test_set_applies_and_persists_override(self, control_path, cfg):
        ControlCenter(control_path, cfg).handle("set BEAR_POSITION_SCALE 0")
        assert cfg.bear_position_scale == 0.0
        fresh = SimpleNamespace(bear_position_scale=0.25, max_concurrent_positions=5, stopout_cooldown_hours=12.0)
        ControlCenter(control_path, fresh)
        assert fresh.bear_position_scale == 0.0

    def test_set_casts_integers(self, control_path, cfg):
        ControlCenter(control_path, cfg).handle("set max_concurrent_positions 3")
        assert cfg.max_concurrent_positions == 3 and isinstance(cfg.max_concurrent_positions, int)

    @pytest.mark.parametrize("command", ["set ATR_STOP_MIN_PCT 0.01", "set BEAR_POSITION_SCALE 2",
                                         "set MAX_CONCURRENT_POSITIONS -1", "set BEAR_POSITION_SCALE abc",
                                         "set BEAR_POSITION_SCALE"])
    def test_set_rejects_unknown_or_out_of_bounds(self, control_path, cfg, command):
        reply = ControlCenter(control_path, cfg).handle(command)
        assert reply.lower().startswith("error")
        assert cfg.bear_position_scale == 0.25 and cfg.max_concurrent_positions == 5

    def test_unset_restores_default(self, control_path, cfg):
        center = ControlCenter(control_path, cfg)
        center.handle("set BEAR_POSITION_SCALE 0")
        center.handle("unset BEAR_POSITION_SCALE")
        assert cfg.bear_position_scale == 0.25
        assert ControlCenter(control_path, cfg).overrides() == {}

    def test_flatten_pauses_before_selling(self, control_path, cfg):
        seen = {}
        center = ControlCenter(control_path, cfg)
        center.flatten_fn = lambda: seen.setdefault("paused_during_flatten", center.is_paused()) or "sold"
        center.handle("flatten")
        assert seen["paused_during_flatten"] is True
        assert center.is_paused()

    def test_status_includes_control_state(self, control_path, cfg):
        center = ControlCenter(control_path, cfg)
        center.status_fn = lambda: "Regime: BEAR"
        center.handle("pause testing")
        center.handle("set BEAR_POSITION_SCALE 0.1")
        reply = center.handle("status")
        assert "Regime: BEAR" in reply and "PAUSED" in reply and "BEAR_POSITION_SCALE=0.1" in reply

    def test_unknown_command_returns_help(self, control_path, cfg):
        assert "pause" in ControlCenter(control_path, cfg).handle("dance")

    def test_corrupt_control_file_fails_safe_to_paused(self, control_path, cfg):
        control_path.write_text("{not json")
        assert ControlCenter(control_path, cfg).is_paused()


class TestUnixSocket:
    def test_round_trip_and_permissions(self, tmp_path, cfg):
        # AF_UNIX paths are limited to ~104 bytes on macOS, so keep the socket path short.
        sock = Path("/tmp") / f"tb-test-{os.getpid()}.sock"
        center = ControlCenter(tmp_path / "control.json", cfg)

        async def scenario():
            server = await serve_unix(sock, center)
            try:
                reader, writer = await asyncio.open_unix_connection(str(sock))
                writer.write(b"pause from test\n")
                await writer.drain()
                reply = (await reader.readline()).decode()
                writer.close()
                return reply, stat.S_IMODE(os.stat(sock).st_mode)
            finally:
                server.close()
                await server.wait_closed()

        try:
            reply, mode = asyncio.run(scenario())
        finally:
            sock.unlink(missing_ok=True)
        assert "paused" in reply.lower()
        assert mode == 0o600
        assert center.is_paused()


def _update(update_id, text, chat_id=CHAT_ID, from_id=CHAT_ID):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "from": {"id": from_id}, "text": text}}


class TestTelegramPoller:
    def _poller(self, control_path, cfg, updates):
        center = ControlCenter(control_path, cfg)
        center.flatten_fn = MagicMock(return_value="sold")
        session = MagicMock()
        session.get.return_value.json.return_value = {"ok": True, "result": updates}
        replies = []
        poller = TelegramPoller(center, token="T", chat_id=CHAT_ID, session=session, send=replies.append)
        return center, poller, session, replies

    def test_only_owner_commands_are_handled(self, control_path, cfg):
        updates = [_update(1, "/pause", chat_id=999), _update(2, "/pause", from_id=999)]
        center, poller, _, replies = self._poller(control_path, cfg, updates)
        poller.poll_once()
        assert not center.is_paused() and replies == []

    def test_owner_pause_is_handled_and_answered(self, control_path, cfg):
        center, poller, _, replies = self._poller(control_path, cfg, [_update(5, "/pause")])
        poller.poll_once()
        assert center.is_paused() and len(replies) == 1

    def test_flatten_requires_confirm(self, control_path, cfg):
        center, poller, _, replies = self._poller(control_path, cfg, [_update(1, "/flatten")])
        poller.poll_once()
        center.flatten_fn.assert_not_called()
        assert not center.is_paused() and "confirm" in replies[0]

    def test_flatten_confirm_runs(self, control_path, cfg):
        center, poller, _, _ = self._poller(control_path, cfg, [_update(1, "/flatten confirm")])
        poller.poll_once()
        center.flatten_fn.assert_called_once()

    def test_backlog_is_skipped_on_start(self, control_path, cfg):
        center, poller, session, _ = self._poller(control_path, cfg, [_update(41, "/flatten confirm")])
        poller.skip_backlog()
        center.flatten_fn.assert_not_called()
        session.get.return_value.json.return_value = {"ok": True, "result": []}
        poller.poll_once()
        assert session.get.call_args.kwargs["params"]["offset"] == 42
