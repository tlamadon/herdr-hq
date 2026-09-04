# herdr HQ

A small web dashboard that SSHes into your machines, pulls each one's [herdr](https://herdr.dev)
session state — every agent, its status, its workspace — plus CPU and memory usage
attributed to the individual agent sessions, and shows the whole fleet on one page.

Plain HTML/CSS/JS in the browser (no build step, no CDN), an asyncio Python server
([Starlette](https://starlette.io) + [asyncssh](https://asyncssh.readthedocs.io)), and
nothing at all installed on the remote machines beyond `python3` and `herdr` itself —
the collector is piped to a bare remote interpreter over ssh.

```
uv tool install herdr-hq     # or: pip install herdr-hq
herdr-hq                     # then open the printed http://127.0.0.1:8787/?token=... URL
```

Every run is protected by a login token (printed at startup) or a permanent
`listen.password` from the config; see **Auth** below.

## How agent resource usage is attributed

herdr's socket API reports the shell PID backing every pane (`herdr pane process-info`).
The collector walks the descendant process tree of each pane shell and charges every
process in it to that pane's agent — so a Claude Code session's CPU includes the model
client, the shells it spawns, the test runs, everything under it.

CPU is a true instantaneous reading: the collector samples cumulative CPU time twice,
~0.7 s apart, and divides the delta by wall time. `100%` means one core saturated, so an
agent running a parallel build can legitimately read `450%`. Memory is summed RSS, which
counts shared pages once per process — agent totals can therefore exceed the machine's
own "used" figure.

## Git state

Each agent card also shows the repository it is working in:

```
myproject · feat/new-solver  [worktree]  ↑2 ↓1  ● 3 changed
```

- **Repo** is the *main* repository name, resolved through `--git-common-dir`, so an agent
  in a linked worktree is still filed under the repo it belongs to.
- **Branch** is the checked-out branch, or `detached`.
- **`worktree`** marks a linked worktree; hover it for the checkout path.
- **↑/↓** are commits ahead of / behind the upstream branch. These come from the local
  remote-tracking ref — **the collector never runs `git fetch`**, so "behind" is accurate
  as of that repo's last fetch. No network, no side effects.
- **changed** counts staged + modified + untracked + conflicted paths; hover for the
  breakdown. `clean` when there is nothing to commit.

All of it comes from two read-only commands per distinct directory (`git rev-parse` and
`git status --porcelain=v2 --branch`), run with `--no-optional-locks` and
`GIT_OPTIONAL_LOCKS=0` so polling can never contend for `index.lock` with a git command
the agent is running itself. Directories are de-duplicated first, so five agents sharing
one repo cost one lookup. Repo, branch and worktree name are all searchable in the filter
box.

## Terminals

Every agent card has a **⌨** button that opens a live xterm.js view of that pane, on any
machine, without leaving the browser. It's writable — click the screen and type, same as a
terminal. Untick **Send keys** to hold it read-only while you watch.

herdr publishes no raw output stream, so this is a *mirror* rather than a PTY attach:
`attach.py` polls `pane.read` for the visible screen with ANSI intact (4×/s by default)
and pushes a frame whenever it changes. The browser repaints in place — cursor home, erase
to end of line per row — which is flicker-free. The local cursor is hidden, because herdr
doesn't tell us where the real one is.

- **Output** streams to the browser over SSE (`/api/term/stream`).
- **Input** is POSTed to `/api/term/input` and coalesced, so a burst of typing or a paste
  is one request.
- **Keys are translated, not forwarded.** herdr takes *named* keys — a raw `\x15` byte
  shows up in the pane as a literal `^U` — so xterm's byte stream is split into printable
  runs (`text`) and key names (`Ctrl+U`, `Enter`, `Shift+Tab`, `Alt+Left`, …). herdr 0.8.2
  has no name for **Delete, Home, End, PageUp or PageDown**; those keys are dropped and
  the footer says so.
- Mirrors are shared: two browsers on the same pane use one ssh connection. A mirror with
  no viewers is torn down after 20s.
- `?termhost=<host>&termpane=<pane>` deep-links straight into a terminal.

Typing goes to a **live agent session** — the same buffer the agent is reading, so a stray
Enter can answer a permission prompt. Keys only flow once you click into the screen, so the
open modal never swallows keystrokes on its own. Two switches in the config lock it down
further: `terminal.input: false` (view-only, the checkbox is disabled) or
`terminal.enabled: false` (no terminals at all).

One way this is unlike a local terminal: there's no cursor (herdr doesn't report its
position) and the mirror runs ~250 ms behind, so typing is slightly blind.

`static/vendor/xterm.js` is vendored so the dashboard works offline; nothing is fetched
from a CDN at runtime.

## Listening ports

Any TCP port a pane's process tree is listening on shows up as a chip — dev servers,
notebooks, preview builds an agent started for itself:

```
LISTENING  :5173  :8000
```

Attribution reuses the same process trees as CPU and memory, so a port is credited to the
pane whose shell is its ancestor. Agent cards show their own ports; each machine card
shows **Pane ports**, which covers every herdr pane on that host, agent or not.

A chip is a **link when the dashboard can actually reach it directly** — anything on
the machine running the server, or a remote port bound past loopback. A remote port on
`127.0.0.1` becomes a dashed **proxy chip**: one click opens

```
http://p8877.<host>.localhost:8787/
```

— a live preview of the remote app, reverse-proxied through the same pooled SSH
connection, no declaration needed. Every service lives on its own `*.localhost`
origin at path `/`, so apps that generate absolute paths (Jupyter, Grafana, Vite…)
work unmodified, WebSockets included, and cookies stay isolated per app. Services can
also be declared permanently per host in the config (`http: {jupyter: 8888}`) and then
appear in the Browse view's Services panel. When two checkouts of the same repo bind
the same port number, the chips carry the worktree/branch name to tell them apart.

The proxy origins are covered by the same auth: service links carry a one-time token
that sets each origin's cookie, and bookmarked service URLs bounce through the main
origin to pick it up (`/__bless`).

How it's found, without root and without extra tooling:

- **Linux** — `/proc/net/tcp{,6}` gives the LISTEN sockets and their inodes; the collector
  then reads `/proc/<pid>/fd` for the pane processes only and matches `socket:[inode]`.
  Scanning just the pane trees rather than every process keeps this cheap.
- **macOS** — `lsof -nP -iTCP -sTCP:LISTEN` restricted to those same pids.

Limits: TCP only (no UDP), and on Linux only the network namespace the collector runs in —
a port inside a container or a private netns won't appear.

## Live status and the inbox

Status changes stream in **live**: a tiny stdlib script per host holds a
`events.subscribe` connection to herdr's socket and relays events back over the same
ssh channel, so a pane flipping to `blocked` shows up in under a second — the topbar
says `· push` while the stream is healthy, and the regular poll (which owns CPU, RAM,
git and ports) is the automatic fallback.

The dashboard sorts by **attention** by default: a cross-host *Needs attention* band
leads with agents that are blocked or that finished while you weren't looking
(herdr's own `done`-vs-`idle` distinction). Unread agents carry a dot until you open
their terminal, click the card, or hit *Mark seen*; the summary splits *Done ·
unseen* from *Idle*.

## Transcript peek

Each agent card shows **what the agent is actually doing** — the tool it is running
right now, or the last thing it said — expandable to the last prompt and answer. The
line comes from tailing the agent's own session transcript (e.g. Claude Code's JSONL)
over SFTP, cached by `(mtime, size)`. The exact session comes from herdr's agent
integration when installed (`herdr integration install claude`); otherwise the newest
transcript for the pane's working directory is used. `GET /api/transcript` serves the
summary, `GET /api/transcript/messages` the chat view's full message list.

## The Workspace view

The second view (topbar → **Workspace**, `/work`) is where you *work with* an agent
rather than watch it. A left sidebar lists **projects**, expanding into their
checkouts — `branch @ machine`, worktrees marked — plus a **Machines** section for
panes outside any repo. Selecting a checkout puts its herdr panes in a **tab strip**;
the active tab is a live terminal mirror, and agent panes toggle **Terminal ⇄ Chat**:

- **Chat** renders the agent's own session transcript as a conversation — markdown
  with **KaTeX math**, **highlighted code**, tables — with tool calls as compact rows
  (`▸ Bash · pytest -q`). A composer at the bottom sends your text straight into the
  live pane (Enter included), so you can steer the agent like any chat app. The
  transcript comes from herdr's reported agent session when available, else from the
  newest Claude Code log for the pane's working directory.
- **Files** is a per-checkout browser (SFTP); files open in the live viewer.
- The right panel shows the **files the agent touched** (from its tool calls, newest
  first, one click into the viewer), the checkout's **listening ports** (same
  direct/proxy chips as the dashboard), plus live views and tunnels.

Every agent card on the dashboard has a **🗀** button that deep-links here.

Files open in a **live viewer** (`/view`) that re-renders in place whenever the remote
file settles on a new `(mtime, size)` — a `latexmk` still mid-write never renders as
garbage, and your scroll position survives every refresh:

- **PDFs** render with a vendored pdf.js, with clickable hyperlinks and internal links.
- **Markdown** renders with a vendored `marked`, sanitized through DOMPurify; relative
  images and links inside a remote README resolve through the file API.
- **Images** and **text/code/log files** render directly.

Open live views are listed on the Browse page (with a cross-tab "Recent views"
history), and **Tunnels** opens raw TCP forwards `127.0.0.1:<local> → host:<port>`
through the pooled connection — they self-heal when a connection drops and reconnects.

## Configuring machines

Configuration lives in `herdr-hq.yaml`, looked up as `$HERDRHQ_CONFIG`, then
`./herdr-hq.yaml`, then `~/.config/herdr-hq/herdr-hq.yaml`. A legacy herdr-hq 0.1
`./config.json` is still read (with a migration hint logged). See
`herdr-hq.example.yaml` for the full commented template:

```yaml
listen:
  host: 127.0.0.1
  port: 8787
  # password: s3cret        # permanent secret instead of the per-run token

poll:
  interval: 6.0             # seconds between collector runs per host
  sample_interval: 0.7      # CPU sampling window; longer is steadier, slower
  history: 120              # samples kept per host for the sparklines

hosts:
  laptop:
    transport: local        # the machine running the server
  workstation: {}           # ssh host; target defaults to the name
  buildbox:
    target: you@192.0.2.10  # anything ssh accepts; a ~/.ssh/config alias is ideal
    python: /usr/bin/python3
```

### SSH requirements

The server connects with [asyncssh](https://asyncssh.readthedocs.io), which reads
`~/.ssh/config`, `~/.ssh/known_hosts` and the ssh-agent (`SSH_AUTH_SOCK`) directly —
anything you can reach with a plain, prompt-free `ssh <target>` works:

```
ssh workstation true          # must succeed silently
```

Load your key into the agent first (`ssh-add ~/.ssh/id_ed25519`) or use a 1Password /
`IdentityAgent` socket, and connect once by hand so `known_hosts` has the host key.
One multiplexed connection per host is kept open and reused for everything — polls,
terminals, files — so only the first poll pays the handshake cost. Host-specific
options (ports, jump hosts, users) belong in `~/.ssh/config`, not here.

A host that can't be reached shows up as `unreachable` with the ssh error on its card;
the rest of the fleet keeps updating.

## The dashboard

- **Fleet summary** — total agents, how many are working / blocked / idle, and the total
  CPU (in cores) and memory the agents are consuming across all machines.
- **Machines** — per host: CPU / RAM / disk meters, a CPU sparkline, load, uptime,
  agent count, herdr version, poll latency, and every port herdr's panes are listening on.
- **Projects** — the same agents rolled up by repository, so you see the work rather than
  the hardware. A project spans machines (`dotfiles` checked out on two boxes is one card);
  a *checkout* does not, so the main clone and each linked worktree are listed separately
  with their own branch, ahead/behind and dirty count. Header badges flag unpushed commits
  and dirty checkouts. Click a project name to filter the agent list to it.
- **Agents** — one card per agent, grouped by machine: status, terminal title, working
  directory, repo / branch / worktree / dirty state, listening ports, live CPU and RSS
  with a sparkline, and an expandable list of the heaviest processes in that agent's tree.
- Filter by status or free text, sort by status / CPU / memory / title, switch to a
  **table view**, and toggle light/dark.

Status comes straight from herdr: `working`, `blocked` (waiting on you), `idle`, `done`,
`unknown`. Each is shown with an icon and a label, never colour alone.

## Layout

```
src/herdrhq/
  app.py               HTTP routes + app factory
  fleet.py             per-host poll loops
  term.py              terminal session bridge
  transport.py         runs the remote scripts, locally or over ssh
  pool.py              one multiplexed asyncssh connection per host
  auth.py              cookie/token login gate
  config.py            YAML config (+ legacy config.json migration)
  remote/collector.py  runs on each machine; prints one JSON blob (stdlib only)
  remote/attach.py     runs on each machine; mirrors one pane, forwards keys
  static/              index.html, style.css, app.js, term.js, vendor/xterm.js
herdr-hq.example.yaml  commented config template
```

The remote scripts are piped to the remote interpreter over stdin, so nothing is ever
installed or left behind on the machines you poll. The collector works standalone too:

```
python3 src/herdrhq/remote/collector.py --pretty              # this machine
ssh workstation python3 - --interval 1 < src/herdrhq/remote/collector.py
```

## Docker

Images are built by GitHub Actions and published to GHCR on every push to `main`:

```
docker run --rm -p 8787:8787 \
  -v "$PWD/herdr-hq.yaml:/home/hq/.config/herdr-hq/herdr-hq.yaml:ro" \
  -v "$HOME/.ssh:/home/hq/.ssh:ro" \
  ghcr.io/<owner>/herdr-hq:latest
```

Two things to get right, both consequences of the container being its own machine:

- **SSH material must come from the host.** The image ships no keys. Mount `~/.ssh`
  read-only so asyncssh gets your keys *and* `known_hosts` — an unknown host key fails
  rather than prompting. Passphrase-protected keys need the agent forwarded instead:
  `-v "$SSH_AUTH_SOCK:/ssh-agent" -e SSH_AUTH_SOCK=/ssh-agent` (fiddly on macOS Docker;
  an unencrypted per-dashboard key is simpler).
- **A `transport: local` host means the container, not your laptop**, and herdr isn't
  in there. In Docker, list every machine as an `ssh` host — including the one running
  Docker.

The container binds `0.0.0.0` inside its namespace; publish it to `127.0.0.1:8787:8787`
rather than `-p 8787:8787` if you don't want it on your LAN. Set a `listen.password` so
browser logins survive container restarts.

## HTTP API

| route | |
| --- | --- |
| `GET /api/state` | full fleet state — every host, machine stats, agents, histories |
| `POST /api/refresh` | wake every poller immediately |
| `GET /api/term/stream?host=&pane=` | SSE stream of screen frames for one pane |
| `POST /api/term/input` | `{host, pane, ops: [{text}\|{key}]}` — send keystrokes |
| `POST /api/term/close` | `{host, pane}` — tear the mirror down now |
| `GET /api/hosts` | configured + connected hosts, with agent counts |
| `GET /api/fs/ls?host=&path=` | directory listing (path defaults to the remote home) |
| `GET /api/fs/file?host=&path=` | stream file bytes (`&dl=1` forces download) |
| `GET /api/fs/watch?host=&path=` | SSE: fires when `(mtime, size)` settles on a new value |
| `GET /api/views` · `DELETE /api/views/{id}` | open live views; detach one |
| `GET/POST /api/forwards` · `DELETE /api/forwards/{id}` | raw TCP tunnels |
| `GET /api/services` | declared HTTP services with their proxy URLs |
| `POST /api/preview` | `{host, port}` → ad-hoc proxied preview URL |
| `GET /api/state/stream` | SSE push channel: pane patches, poll completions, bridge health |
| `GET /api/transcript?host=&pane=` | what that agent is doing, from its session transcript |

API calls authenticate with the session cookie, `Authorization: Bearer <secret>`, or a
stateless `?token=<secret>` query parameter.

```
herdr-hq --once   # poll each host once, print the JSON, exit
```

## Auth

Loopback TCP ports have no unix permissions: without auth, every local user (and, via
DNS rebinding, hostile web pages) could read your fleet state and type into your agents.
So every run is protected by a shared secret: `listen.password` from the config if set
(survives restarts, so browser cookies stay valid), otherwise a fresh token printed with
the startup URL. First visit sets a 30-day cookie. `listen.auth: none` opts out.

## Notes and limits

- The server binds `127.0.0.1` by default. The JSON exposes working directories,
  terminal titles and process command lines; terminals can type into live agent
  sessions. Auth is on by default — keep it that way anywhere beyond loopback, and put
  a real reverse proxy with TLS in front for anything public.
- On macOS the collector uses `/bin/ps` explicitly; a `procps` build of `ps` earlier on
  `PATH` (common under nix) refuses to report RSS.
- `herdr-hq.yaml` and `config.json` are gitignored — they hold your machine list.
- `src/herdrhq/static/vendor/xterm.js` is a vendored copy of
  [xterm.js](https://xtermjs.org) 5.5.0, MIT licensed; see `vendor/LICENSE-xterm.txt`.
