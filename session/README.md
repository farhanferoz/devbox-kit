# Persistent remote sessions

One short command to open, list and close terminal sessions on a remote Linux box.
The sessions live on the box (they are [shpool](https://github.com/shell-pool/shpool)
sessions), so a dropped Wi-Fi link, a closed laptop lid or switching machines does not
kill what is running: attach again and you are back where you were.

```
box              attach to the session "main" (creates it if needed)
box train        attach to the session "train"
box ls           list sessions and whether each is attached
box detach       disconnect every attached session without killing it
box kill train   end the session "train" and everything running in it
box status       uptime, memory and GPU use of the box
```

`box` is whatever alias you choose (step 3). Inside a session, press
**Ctrl-Space then Ctrl-q** to detach and leave it running.

Taking over: attaching to a session that another terminal holds takes it away from
that terminal. That is deliberate, so a session left attached on a machine you walked
away from never locks you out.

## 1. On the box (the server)

Install shpool and run its daemon as a user service. These are the upstream
instructions from shpool's README (for version 0.11.3):

```bash
cargo install shpool
curl -fLo "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/shpool.service" --create-dirs \
  https://raw.githubusercontent.com/shell-pool/shpool/master/systemd/shpool.service
sed -i "s|/usr|$HOME/.cargo|" "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/shpool.service"
curl -fLo "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/shpool.socket" --create-dirs \
  https://raw.githubusercontent.com/shell-pool/shpool/master/systemd/shpool.socket
systemctl --user enable shpool
systemctl --user start shpool
loginctl enable-linger      # keep the daemon (and your sessions) alive after you log out
```

No Rust? Install it first with [rustup](https://rustup.rs). Check: `shpool list` prints
a `NAME STATUS` header.

## 2. On each machine you connect from

Give the box an SSH alias in `~/.ssh/config` and use key login, so `ssh mybox` works
with no password prompt:

```
Host mybox
    # the box's address or DNS name (a Tailscale name works too)
    HostName 192.0.2.10
    User yourname
    # notice a dead link within about 90 seconds (30 s x 3 missed replies)
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

Check: `ssh mybox hostname` prints the box's hostname without asking for a password.
That printed name is what `REMOTE_SESSION_SERVER` needs in step 3.

Apart from this repo (step 3), the connecting machine needs only `ssh`.

## 3. Load the command (connecting machines AND the box itself)

Clone this repo (the lines below assume `~/dev/devbox-kit`; change the path if you put
it elsewhere), then add to `~/.bashrc` (Linux) or `~/.zshrc` (macOS):

```bash
export REMOTE_SESSION_HOST=mybox        # the ssh alias from step 2
export REMOTE_SESSION_SERVER=halo       # what `hostname` prints ON the box
source ~/dev/devbox-kit/session/remote_session.sh
alias box=remote_session
```

On the box itself the same lines work: when `hostname` matches
`REMOTE_SESSION_SERVER`, commands run locally instead of over ssh.

Open a new terminal (or `source ~/.bashrc`), then check: `box help` prints the command
list, `box ls` prints a `NAME STATUS` header, and `box` opens the session `main`.

## Notes

- `status` shows NVIDIA GPUs with `nvidia-smi` and AMD GPUs with `amd-smi metric --usage`
  (from AMD's ROCm SMI documentation). The AMD line has not been tried on a
  Strix Halo machine yet; if `amd-smi` is not installed, `status` says so and still
  prints uptime and memory.
- Session names may contain only letters, digits, `.`, `_` and `-`. The name ends up
  inside a command run on the box, so anything else is refused rather than quoted.
- `kill` has no default session on purpose: a forgotten name must not end `main`.
