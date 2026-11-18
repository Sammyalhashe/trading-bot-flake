#!/usr/bin/env python3
"""Send a control command to the running trading bot, e.g. `trading-botctl pause reviewing`."""
import html
import os
import re
import socket
import sys


def socket_path() -> str:
    return os.environ.get("TRADING_BOT_SOCKET") or os.path.join(
        os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "trading-bot.sock")


def main() -> int:
    command = " ".join(sys.argv[1:]) or "status"
    path = socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(300)  # flatten waits for any in-flight scan, then sells
            sock.connect(path)
            sock.sendall(command.encode() + b"\n")
            chunks = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"trading bot is not running (no socket at {path})", file=sys.stderr)
        return 1
    reply = b"".join(chunks).decode(errors="replace")
    print(html.unescape(re.sub(r"</?[a-z]+>", "", reply)).rstrip())
    return 1 if reply.lower().startswith("error") else 0


if __name__ == "__main__":
    sys.exit(main())
