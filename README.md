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
.\.venv\Scripts\python.exe -m lanmsg.server --host 0.0.0.0 --port 8000
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
- IRC-inspired compact message rows, timestamps, nicknames, and date separators.
- Server-wide appearance editor with presets, live preview, and persistent CSS tokens.
- Editable display names with stable account IDs and unique sign-in handles.

## Display names

Click your name at the bottom of the channel sidebar to change your display name.
Names can contain spaces and visible Unicode characters and need not be unique.
The unique account username remains your sign-in handle. Click a nickname in chat
to see the account's `@handle` and distinguish people with matching display names.
This displays handles; mention parsing and notifications are not implemented yet.

Name changes update earlier messages and connected clients immediately; account
IDs, message authorship, permissions, and sign-in credentials stay attached to the
same account. Existing databases upgrade automatically and use each username as
the initial display name. Broader profile management remains a follow-up.

## Appearance

The server owner can open **Appearance** in the channel sidebar. Adjust colors,
monospace/system fonts, text size, line height, message row spacing, and corner
radius. Choose Midnight IRC, Terminal green, or Daylight as a starting palette.
Changes preview locally while the editor is open. **Cancel** or Escape restores
the saved theme; **Save for everyone** persists it in SQLite and updates connected
clients immediately. **Reset to defaults** previews the default palette until saved.
The sign-in screen also uses the server theme.

Design tokens are declared at the top of `lanmsg/static/style.css`, including
`--background`, `--sidebar`, `--panel`, `--text`, `--muted`, `--accent`, `--border`,
`--nickname`, `--font-family`, `--font-size`, `--line-height`, `--row-spacing`,
and `--corner-radius`. Owners customize these through validated settings rather
than arbitrary CSS. Saved overrides take precedence over defaults in the CSS
file. Structural variables such as `--sidebar-width` and `--nickname-width`
remain available for source-level layout adjustments.

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
