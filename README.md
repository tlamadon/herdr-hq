# herdr HQ

A small web dashboard that SSHes into your machines, pulls each one's [herdr](https://herdr.dev)
session state — every agent, its status, its workspace — plus CPU and memory usage
attributed to the individual agent sessions, and shows the whole fleet on one page.

No dependencies: Python 3 standard library on the server, plain HTML/CSS/JS in the browser,
and nothing at all installed on the remote machines beyond `python3` and `herdr` itself.

```
./server.py            # then open http://127.0.0.1:8787/
```

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
open modal never swallows keystrokes on its own. Two switches in `config.json` lock it down
further: `"terminal_input": false` (view-only, the checkbox is disabled) or
`"terminal_enabled": false` (no terminals at all).

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

A chip is a **link when the dashboard can actually reach it** — anything on the machine
running the server, or a remote port bound past loopback. A remote port on `127.0.0.1`
stays a plain chip, and its tooltip gives you the tunnel to open:

```
ssh -L 8877:localhost:8877 buildbox
```

How it's found, without root and without extra tooling:

- **Linux** — `/proc/net/tcp{,6}` gives the LISTEN sockets and their inodes; the collector
  then reads `/proc/<pid>/fd` for the pane processes only and matches `socket:[inode]`.
  Scanning just the pane trees rather than every process keeps this cheap.
- **macOS** — `lsof -nP -iTCP -sTCP:LISTEN` restricted to those same pids.

Limits: TCP only (no UDP), and on Linux only the network namespace the collector runs in —
a port inside a container or a private netns won't appear.

## Configuring machines

On first run the server copies `config.example.json` to `config.json`. Edit the `hosts` list:

```json
{
  "host": "127.0.0.1",
  "port": 8787,
  "poll_interval": 6.0,
  "sample_interval": 0.7,
  "ssh_timeout": 30.0,
  "history": 120,
  "terminal_enabled": true,
  "terminal_input": true,

  "hosts": [
    { "name": "laptop",   "transport": "local" },
    { "name": "workstation", "transport": "ssh", "target": "workstation" },
    { "name": "buildbox",    "transport": "ssh", "target": "you@192.0.2.10",
      "python": "/usr/bin/python3", "ssh_args": ["-p", "2222"] }
  ]
}
```

| key | meaning |
| --- | --- |
| `name` | label shown in the UI |
| `transport` | `local` (the machine running the server) or `ssh` |
| `target` | anything `ssh` accepts — a `~/.ssh/config` host alias is ideal |
| `python` | interpreter to run the collector with on that host (default `python3`) |
| `ssh_args` | extra flags appended to the ssh command |
| `connect_timeout` | ssh connect timeout in seconds (default 8) |
| `poll_interval` | seconds between polls of every host |
| `sample_interval` | CPU sampling window on each host; longer is steadier, slower |
| `history` | samples kept per host for the sparklines |

### SSH requirements

Hosts are polled with `BatchMode=yes`, so **key auth must work without a prompt**:

```
ssh workstation true          # must succeed silently
```

Load your key into the agent first (`ssh-add ~/.ssh/id_ed25519`) or use a 1Password /
`IdentityAgent` socket. Connections are multiplexed (`ControlMaster=auto`,
`ControlPersist=180`), so only the first poll pays the handshake cost — subsequent
polls reuse the same connection and take tens of milliseconds.

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
server.py            web server, per-host poll loops, terminal session bridge
collector.py         runs on each machine; prints one JSON blob (stdlib only)
attach.py            runs on each machine; mirrors one pane, forwards keys
static/              index.html, style.css, app.js, term.js, vendor/xterm.js
config.example.json  template copied to config.json on first run
```

`collector.py` is piped to the remote interpreter over stdin, so nothing is ever
installed or left behind on the machines you poll. It works standalone too:

```
python3 collector.py --pretty              # this machine
ssh workstation python3 - --interval 1 < collector.py
```

## Docker

Images are built by GitHub Actions and published to GHCR on every push to `main`:

```
docker run --rm -p 8787:8787 \
  -v "$PWD/config.json:/app/config.json:ro" \
  -v "$HOME/.ssh:/home/hq/.ssh:ro" \
  ghcr.io/<owner>/herdr-hq:latest
```

Two things to get right, both consequences of the container being its own machine:

- **SSH material must come from the host.** The image ships an ssh client but no keys.
  Mount `~/.ssh` read-only so it gets your keys *and* `known_hosts` — polling uses
  `BatchMode=yes`, so an unknown host key fails rather than prompting. If your key needs a
  passphrase, forward the agent instead: `-v "$SSH_AUTH_SOCK:/ssh-agent" -e SSH_AUTH_SOCK=/ssh-agent`.
- **A `"transport": "local"` host means the container, not your laptop**, and herdr isn't
  in there. In Docker, list every machine as an `ssh` host — including the one running
  Docker.

The container binds `0.0.0.0` inside its namespace; publish it to `127.0.0.1:8787:8787`
rather than `-p 8787:8787` if you don't want it on your LAN. There is no authentication —
see the note at the end.

## HTTP API

| route | |
| --- | --- |
| `GET /api/state` | full fleet state — every host, machine stats, agents, histories |
| `POST /api/refresh` | wake every poller immediately |
| `GET /api/term/stream?host=&pane=` | SSE stream of screen frames for one pane |
| `POST /api/term/input` | `{host, pane, ops: [{text}\|{key}]}` — send keystrokes |
| `POST /api/term/close` | `{host, pane}` — tear the mirror down now |

```
./server.py --once   # poll each host once, print the JSON, exit
```

## Notes and limits

- The server binds `127.0.0.1` by default. It has no authentication and the JSON exposes
  working directories, terminal titles and process command lines — bind it to a public
  interface only behind something that authenticates.
- Terminals make that sharper: anyone who can reach the port can type into your agents.
  Keep it on loopback, or set `"terminal_input": false`.
- On macOS the collector uses `/bin/ps` explicitly; a `procps` build of `ps` earlier on
  `PATH` (common under nix) refuses to report RSS.
- `config.json` is gitignored — it holds your machine list.
- `static/vendor/xterm.js` is a vendored copy of [xterm.js](https://xtermjs.org) 5.5.0,
  MIT licensed; see `static/vendor/LICENSE-xterm.txt`.
