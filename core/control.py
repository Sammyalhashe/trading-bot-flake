"""Runtime control of the live bot: pause/resume, flatten, and allowlisted setting overrides.

State lives in its own JSON file so the WS tick path, which saves whole cached snapshots of
trading_state.json, can never overwrite a pause.
"""
import asyncio
import datetime
import html
import json
import logging
import os
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# PARAM -> (config attribute, type, min, max)
OVERRIDABLE = {
    "BEAR_POSITION_SCALE": ("bear_position_scale", float, 0.0, 1.0),
    "MAX_CONCURRENT_POSITIONS": ("max_concurrent_positions", int, 0, 10),
    "STOPOUT_COOLDOWN_HOURS": ("stopout_cooldown_hours", float, 0.0, 168.0),
}

HELP = ("Commands: status | pause [reason] | resume | flatten | set NAME VALUE | unset NAME\n"
        f"Settable: {', '.join(OVERRIDABLE)}")


class ControlCenter:
    def __init__(self, path: Path, config, status_fn=None, flatten_fn=None):
        self.path = Path(path)
        self.config = config
        self.status_fn = status_fn
        self.flatten_fn = flatten_fn
        self._lock = threading.Lock()
        self._defaults = {name: getattr(config, attr) for name, (attr, *_) in OVERRIDABLE.items()}
        self._apply_overrides(self._read().get("overrides", {}))

    def _read(self) -> dict:
        if not self.path.exists():
            return {"paused": False, "overrides": {}}
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError) as e:
            # An unreadable control file must not silently re-enable trading.
            logger.error(f"[Control] Cannot read {self.path}: {e}; treating trading as paused")
            return {"paused": True, "reason": "control file unreadable", "overrides": {}}

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)

    def _apply_overrides(self, overrides: dict) -> None:
        for name, (attr, kind, _, _) in OVERRIDABLE.items():
            setattr(self.config, attr, kind(overrides[name]) if name in overrides else self._defaults[name])

    def is_paused(self) -> bool:
        return bool(self._read().get("paused", False))

    def overrides(self) -> dict:
        return dict(self._read().get("overrides", {}))

    def handle(self, text: str) -> str:
        parts = text.strip().lstrip("/").split()
        if not parts:
            return HELP
        command, args = parts[0].lower(), parts[1:]
        try:
            if command == "status":
                return self._status()
            if command == "pause":
                return self._pause(" ".join(args) or "manual")
            if command == "resume":
                return self._resume()
            if command == "flatten":
                return self._flatten()
            if command == "set":
                return self._set(args)
            if command == "unset":
                return self._unset(args)
        except Exception as e:
            logger.exception(f"[Control] '{command}' failed")
            return f"Error: {command} failed: {html.escape(str(e))}"
        return HELP

    def _update(self, mutate) -> dict:
        with self._lock:
            data = self._read()
            mutate(data)
            self._write(data)
            return data

    def _pause(self, reason: str) -> str:
        def mutate(d):
            d.update(paused=True, reason=reason, since=datetime.datetime.now().isoformat(timespec="seconds"))
        self._update(mutate)
        logger.warning(f"[Control] Trading paused: {reason}")
        return f"Paused ({html.escape(reason)}). New buys stop; stops and exits keep running."

    def _resume(self) -> str:
        self._update(lambda d: d.update(paused=False, reason="", since=""))
        logger.warning("[Control] Trading resumed")
        return "Resumed. New buys allowed from the next scan."

    def _flatten(self) -> str:
        if self.flatten_fn is None:
            return "Error: flatten is only available inside the running bot"
        self._pause("flatten")
        result = self.flatten_fn()
        return f"Paused and sold holdings to USDC. {result or ''}".strip()

    def _set(self, args) -> str:
        if len(args) != 2:
            return "Error: usage: set NAME VALUE"
        name = args[0].upper()
        if name not in OVERRIDABLE:
            return f"Error: {html.escape(name)} is not settable. Settable: {', '.join(OVERRIDABLE)}"
        attr, kind, low, high = OVERRIDABLE[name]
        try:
            value = kind(args[1])
        except ValueError:
            return f"Error: {html.escape(args[1])} is not a valid {kind.__name__}"
        if not low <= value <= high:
            return f"Error: {name} must be between {low} and {high}"
        data = self._update(lambda d: d.setdefault("overrides", {}).update({name: value}))
        self._apply_overrides(data["overrides"])
        logger.warning(f"[Control] {name} set to {value}")
        return f"{name} = {value}"

    def _unset(self, args) -> str:
        if len(args) != 1 or args[0].upper() not in OVERRIDABLE:
            return f"Error: usage: unset NAME ({', '.join(OVERRIDABLE)})"
        name = args[0].upper()
        data = self._update(lambda d: d.setdefault("overrides", {}).pop(name, None))
        self._apply_overrides(data["overrides"])
        logger.warning(f"[Control] {name} reset to default {self._defaults[name]}")
        return f"{name} back to default ({self._defaults[name]})"

    def _status(self) -> str:
        data = self._read()
        lines = [self.status_fn()] if self.status_fn else []
        if data.get("paused"):
            lines.append(f"Trading: PAUSED since {html.escape(data.get('since') or '?')} "
                         f"({html.escape(data.get('reason') or '')})")
        else:
            lines.append("Trading: active")
        overrides = data.get("overrides", {})
        if overrides:
            lines.append("Overrides: " + ", ".join(f"{k}={v}" for k, v in sorted(overrides.items())))
        return "\n".join(lines)


async def serve_unix(path: Path, center: ControlCenter):
    """One request line in, the reply out, then close. Owner-only socket."""
    path = Path(path)
    path.unlink(missing_ok=True)

    async def handle_client(reader, writer):
        try:
            line = (await reader.readline()).decode(errors="replace")
            reply = await asyncio.to_thread(center.handle, line)
            writer.write((reply.rstrip("\n") + "\n").encode())
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handle_client, path=str(path))
    os.chmod(path, 0o600)
    logger.info(f"[Control] Listening on {path}")
    return server


class TelegramPoller:
    """Long-polls the bot's own Telegram updates; only the owner's chat may send commands."""

    def __init__(self, center: ControlCenter, token: str, chat_id, session=requests, send=None):
        self.center = center
        self.token = token
        self.chat_id = int(chat_id)
        self.session = session
        if send is None:
            from core.telegram import send_telegram_message as send
        self.send = send
        self._offset = None

    def _get_updates(self, offset, timeout):
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        response = self.session.get(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                    params=params, timeout=timeout + 10)
        payload = response.json()
        return payload.get("result", []) if payload.get("ok") else []

    def skip_backlog(self) -> None:
        """Acknowledge queued updates unprocessed, so a stale /flatten is never replayed."""
        updates = self._get_updates(-1, 0)
        if updates:
            self._offset = updates[-1]["update_id"] + 1

    def poll_once(self, timeout: int = 30) -> None:
        for update in self._get_updates(self._offset, timeout):
            self._offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat_id = message.get("chat", {}).get("id")
            from_id = message.get("from", {}).get("id")
            if chat_id != self.chat_id or from_id != self.chat_id:
                logger.warning(f"[Control] Ignoring Telegram command from chat={chat_id} user={from_id}")
                continue
            text = (message.get("text") or "").strip()
            words = text.lstrip("/").lower().split()
            if words[:1] == ["flatten"] and words[1:] != ["confirm"]:
                self.send("Send <b>/flatten confirm</b> to pause and sell everything to USDC.")
                continue
            self.send(self.center.handle("flatten" if words[:1] == ["flatten"] else text))

    def run(self, stop: threading.Event) -> None:
        backoff = 5
        backlog_skipped = False
        while not stop.is_set():
            try:
                if not backlog_skipped:
                    self.skip_backlog()
                    backlog_skipped = True
                self.poll_once()
                backoff = 5
            except Exception as e:
                logger.error(f"[Control] Telegram poll failed: {str(e).replace(self.token, '<redacted>')}")
                stop.wait(backoff)
                backoff = min(backoff * 2, 300)
