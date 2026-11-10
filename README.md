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

## Quick start (Mac or Linux, everything in order)

`mybox` below is your SSH alias for the box. Each step ends with a check; stop at the first
one that fails and read that tool's "When it does not work" section.

1. **On the box:** install shpool as a user service, as in
   [session/README.md, step 1](session/README.md#1-on-the-box-the-server).
   Check: `shpool list` prints a `NAME STATUS` header.
2. **On your machine:** give the box an SSH alias with key login,
   [session/README.md, step 2](session/README.md#2-on-each-machine-you-connect-from).
   Check: `ssh mybox hostname` prints the box's name without a password prompt.
3. **On your machine:** clone this repo to `~/dev/devbox-kit` and load the `box` command,
   [session/README.md, step 3](session/README.md#3-load-the-command-connecting-machines-and-the-box-itself).
   Check: `box help` prints the command list.
4. **Copy, paste and screenshots.** macOS: optionally `brew install pngpaste` first. Linux:
   `wl-clipboard` (Wayland) or `xclip` (X11).
   ```bash
   cd ~/dev/devbox-kit/clipboard && ./install.sh mybox --selftest
   ```
   Check: every line of the self-test says `PASS`. Reconnect so the session picks it up.
5. **Voice** (`/voice` in Claude Code). Read [voice/README.md, Security](voice/README.md#security-read-this-first)
   first. macOS: `brew install pulseaudio` first, and allow the microphone prompt.
   ```bash
   cd ~/dev/devbox-kit/voice && ./install.sh mybox
   ```
   Check: it ends with `REAL AUDIO`.

The macOS clipboard path has been verified on Apple Silicon against Ubuntu x86_64,
including Finder-copied JPEG/PNG photos and screenshots. The macOS voice installer still
needs a real-Mac verification run; please report its output, pass or fail.

## Conventions

- Nothing here holds a host name, user name, address or key. Each tool reads its
  settings from environment variables documented in its README.
- Every tool works in bash and zsh, on Linux and macOS clients.
