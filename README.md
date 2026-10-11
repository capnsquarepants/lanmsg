# LANmsg

A focused, self-hosted chat app for small groups. Python server, SQLite storage,
browser interface, and an optional Windows desktop shell. Voice is reserved in
the channel model and interface; no audio functionality is implemented.

The development workspace also has an ignored portable Python runtime at
`.runtime/python/python.exe`. If present, it can run the server directly:
`.\.runtime\python\python.exe -m lanmsg.server`. It is a local development tool,
not part of the repository or a distributable app.

## Run on Windows

Install Python 3.11 or newer, then open PowerShell in this repository:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[desktop,test]"
.\.venv\Scripts\lanmsg.exe --host 0.0.0.0 --port 8000
```

Open http://localhost:8000. Create the first account locally before allowing
other computers to connect: that account becomes the owner. The owner can
create channels and single-use invite codes. Share your machine's LAN IP
(for example, http://192.168.1.20:8000) with other users. If Windows prompts,
allow incoming connections on your private network.

Launch the desktop client in another terminal:

```powershell
.\.venv\Scripts\lanmsg-desktop.exe http://localhost:8000
```

The desktop shell requires Microsoft Edge WebView2 Runtime. It connects to an
existing server; it does not start one. Changing servers currently means
relaunching with a different URL.

## What works in this initial prototype

- Invite-based registration and password sign-in; first account owns the server.
- Persistent channels and text messages, with 50-message history pages.
- Live updates, automatic reconnect, and duplicate-safe send retries.
- Session cookies, salted scrypt passwords, safe plain-text message rendering.
- Owner channel creation and single-use invites; transient unread indicators.

Replies, editing/deletion, Markdown, durable unread positions, account recovery,
rate limiting, installers, and voice remain follow-up work. This is a development
prototype, not a production release. There is no automated LAN discovery yet.

## Server deployment

Install with `pip install .` and run `lanmsg --host 0.0.0.0 --port 8000`.
Use one server process: live WebSocket delivery is in memory. For a VPS, place
the service behind an HTTPS reverse proxy with WebSocket support and preserve
the Host header. Configure trusted proxy headers appropriately. Do not expose
plain HTTP sign-in on the public internet. Finish initial owner registration
before exposing the service. Container and executable packaging will follow.

Data lives in `data/lanmsg.db`, relative to the working directory. Set `LANMSG_DB`
to override it. Back up the database while the server is stopped (SQLite WAL
files must be preserved if copying a running database).

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The implementation follows the official [FastAPI WebSocket documentation](https://fastapi.tiangolo.com/advanced/websockets/)
and [pywebview usage guide](https://pywebview.flowrl.com/guide/usage.html).
