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
- Inline message editing, soft deletion, and owner-only retained version history.
- Live updates, automatic reconnect, and duplicate-safe send retries.
- Session cookies, salted scrypt passwords, safe plain-text message rendering.
- Owner channel creation and single-use invites; transient unread indicators.
- IRC-inspired compact message rows, timestamps, nicknames, and date separators.
- Server-wide appearance editor with presets, live preview, and persistent CSS tokens.
- Editable display names with stable account IDs and unique sign-in handles.
- Last-channel restoration on refresh, stored separately for each account in this browser.

## Message editing and deletion

Use **Edit** on your own messages to change their text inline. Saved edits show an
edited marker and update connected clients immediately. A version check prevents
an older editor from overwriting a newer change. The owner cannot edit someone
else's message.

Authors can delete their own messages; the owner can delete any message. Deletion
leaves a **Message deleted** placeholder. Earlier edits and deleted text remain
in SQLite for owner review, with the actor and time recorded for each change.
Ordinary chat APIs and WebSocket events never include deleted text or prior version contents,
even when retrying the original send. Previously received text cannot be removed
from another person's saved copies.

Only the server owner can use **History** on a message or **Admin console > Message
history** to inspect its current retained text and earlier versions. Versions load
in pages of 50, newest first. Admin currently means the single server owner; there
are no delegated admin roles yet. Database access and backups also contain retained
text. Existing databases migrate automatically. If `--retention-days` is enabled,
expiry permanently removes the message and all its versions; otherwise both are
kept indefinitely. There is no restore action yet.

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

The server owner can open **Admin console → Appearance** in the channel sidebar. Adjust colors,
monospace/system fonts, text size, line height, message row spacing, and corner
radius. Choose Midnight IRC, Terminal green, or Daylight as a starting palette.
Appearance controls are embedded in the fixed-height console, with scrolling content.
Changes preview locally. **Discard changes**, switching tabs, or closing the console restores
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

## Admin console

The first account is the server owner and sees **Admin console** in the sidebar.
The backend checks owner access for every console operation; ordinary members
cannot read private console data or change settings by calling its APIs directly.

- **Settings:** server name, description, pause/resume invite-based registration,
  connection limits, concurrent API request limits, and message rate/burst limits.
- **Channels:** view text channels and create new ones. Names are automatically lowercased, with whitespace converted to dashes.
- **Invites:** create, view, copy by selecting a code, and revoke unused invites.
- **Appearance:** edit the server-wide theme directly within the console.
- **Message history:** inspect retained edits and deleted text by message ID.

Settings persist in SQLite. Server identity and registration controls apply
immediately, with identity updates sent to connected clients. Operational limits
apply when the server next starts; the console shows saved and currently active
values and a restart notice when they differ. Saving does not restart the process
or disconnect users. Existing users can still sign in when registration is paused.
The console does not expose shell execution, file paths, or destructive history
retention controls.

Command-line connection/request limits provide initial defaults until console
settings have been saved; saved console limits take precedence on later starts.
Other deployment controls (listening address, port, HTTPS, database path, retention,
and process memory limits) remain configured outside the console.

Replies, editing/deletion, Markdown, durable unread positions, account recovery,
installers, and voice remain follow-up work. This is a development
prototype, not a production release. There is no automated LAN discovery yet.

## Server deployment

Install with `pip install .` and run `lanmsg --host 0.0.0.0 --port 8000`.
Use one server process: live WebSocket delivery is in memory. For a VPS, place
the service behind an HTTPS reverse proxy with WebSocket support and preserve
the Host header. Configure trusted proxy headers appropriately. Do not expose
plain HTTP sign-in on the public internet. Finish initial owner registration
before exposing the service. Container and executable packaging will follow.

Data lives in `data/lanmsg.db`, relative to the working directory. Set `LANMSG_DB`
to override it. Keep the database on the server's local disk. Messages, accounts,
channels, sessions, invites, and server settings currently use one SQLite file.
Channel history uses an index on `(channel_id, id)` and reads 50 rows per page.
Database work runs in the bounded request thread pool rather than the WebSocket
event loop. SQLite lock waits time out after one second and return a retryable
503 response; committed messages remain protected against duplicate retries.

## Resource protections

Defaults target a small private group:

| Protection | Default |
| --- | --- |
| Concurrent API requests | 32 |
| Concurrent login/signup requests | 2 |
| WebSocket connections | 100 total, 20 per IP, 3 per session |
| API requests per IP | 20/second, burst 100 |
| Login/signup per IP | 5 initially, then one replenished attempt every 12 seconds |
| Message sends per account | 2/second, burst 10 |
| Other authenticated writes per account | 1/second, burst 10 |
| Request body | 32 KiB, including streamed bodies |
| Incoming body wait per chunk | 10 seconds |
| Queued events per connected client | 64 |
| Outgoing WebSocket send timeout | 5 seconds |
| WebSocket heartbeat inactivity | 60 seconds |
| Rate-limit buckets | At most 4,096 |
| Disk space reserve for writes | 50 MiB |

Full request slots return 503; rate limits return 429 with `Retry-After`.
Slow clients or clients whose delivery queues fill are disconnected and can
reconnect to fetch persisted history. WebSocket delivery uses a separate bounded
queue for each client, so one slow reader does not delay other clients. Logout
closes sockets belonging to that session. Expired sessions and invites are removed
at startup and hourly. Accounts retain up to ten sign-in sessions, and invites
expire after seven days with at most 100 unused invites outstanding.

Tune the main admission limits with `--max-connections` and `--max-http`.
The remaining defaults are in `lanmsg/reliability.py`. Limit settings and
delivery state belong to one server process. Behind a proxy, configure trusted
forwarded headers explicitly so IP limits use the actual client address; users
sharing an IP share those limits. App limits cannot replace an ingress proxy's
connection/time limits or operating-system memory limits.

Use HTTPS, a reverse proxy with request and connection timeouts, and a service
manager that restarts failed processes. A starting systemd template is provided
in `deploy/lanmsg.service`; adapt its paths and create its service user first.
Monitor process memory, CPU, disk space, and logs. The free-space guard is a
best-effort check, not a storage quota, and history/access logs grow over time.

History is retained indefinitely by default. To opt into deleting messages older
than a chosen age, pass `--retention-days 90`. Deletion runs at startup and during
hourly cleanup. Take a backup first. SQLite reuses freed pages; deletion does not
necessarily shrink the database file immediately.

## Backups and restore

Use SQLite's online backup API rather than copying a live `.db` file:

```powershell
.\.venv\Scripts\python.exe -m lanmsg.backup backups\lanmsg-snapshot.db
```

Override the source with `--database PATH` or `LANMSG_DB`. Each backup must use a
new filename. The command includes committed WAL data, runs an integrity check,
and refuses to overwrite an existing file. Backup retries stop after 60 seconds.
Schedule it externally and keep a bounded number of snapshots, including copies
off the VPS. Backups contain account and session data and need appropriate access
permissions. Restore by stopping the server and setting `LANMSG_DB` to a snapshot
(or placing it in a new data directory); avoid mixing a restored file with old
`-wal` and `-shm` files. The tests include a restore and sign-in check.

Multiple SQLite files remain an option: a main account database plus per-channel
message files would allow independent channel writes, but cross-file foreign keys,
atomic operations, migrations, and coordinated backups would need application
logic. This pass retains one database until measurements justify that complexity.
See [SQLite WAL](https://www.sqlite.org/wal.html),
[cross-database transaction limits](https://sqlite.org/lang_attach.html), and
[the backup API](https://www.sqlite.org/backup.html).

## Local load test

After installing the test extras, run:

```powershell
.\.venv\Scripts\python.exe tools/load_test.py --users 10 --messages 20 --output benchmarks/local-baseline.json
```

The harness starts an isolated loopback server with a temporary database; it does
not contact or modify an existing server. It exercises real HTTP writes,
WebSocket delivery to every client, and paginated history reads across three
channels. Only provisioning bursts are increased; runtime message/connection
limits use their defaults. It fails on HTTP errors, missing persisted messages,
or missing live delivery. The checked-in baseline persisted 200 messages and
delivered all 200 to each of ten clients at about 15 messages/second, with a send
response p95 of about 147 ms. This is a local Windows measurement with deliberate
pacing and Python allocation tracing, not a maximum-throughput benchmark or a
VPS capacity guarantee. Reported Python heap excludes native allocations and is
not total process memory.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

The implementation follows the official [FastAPI WebSocket documentation](https://fastapi.tiangolo.com/advanced/websockets/)
and [pywebview usage guide](https://pywebview.flowrl.com/guide/usage.html).
