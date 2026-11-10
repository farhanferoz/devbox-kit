# devbox-kit

Tools for working on a remote Linux workstation (an NVIDIA DGX Spark, an AMD
Strix Halo box, or anything else you reach over SSH) from a laptop.

Each folder is one self-contained tool with its own README. Take only the ones you want.

| Folder | What it does | Needs on the box | Needs on your machine |
|---|---|---|---|
| [`session/`](session/README.md) | Persistent terminal sessions: `box`, `box ls`, `box kill <name>`, `box status` | shpool (Rust, user service) | ssh, bash or zsh |
| [`clipboard/`](clipboard/README.md) | Copy from Claude Code on the box to your clipboard; paste screenshots and text into it | python3; Debian/Ubuntu-style `~/.bashrc` | python3; Linux: wl-clipboard or xclip; macOS: optional pngpaste |
| [`voice/`](voice/README.md) | Your microphone for Claude Code `/voice` on the box (no authentication: read its Security section) | Debian or Ubuntu, sudo for apt | Linux: PipeWire; macOS: Homebrew PulseAudio |
| [`tunnels/`](tunnels/README.md) | One always-on SSH connection carrying the two above (installed by them) | sshd | Linux: systemd; macOS: launchd |

## Conventions

- Nothing here holds a host name, user name, address or key. Each tool reads its
  settings from environment variables documented in its README.
- Every tool works in bash and zsh, on Linux and macOS clients.
