# Tunnels: one always-on SSH connection that carries the bridges' ports

The clipboard bridge (port 18377) and the voice bridge (port 4713) run on the machine you sit
at and are reached from the box through SSH reverse tunnels. `install.sh` keeps ONE ssh
connection to the box up for them, as a service, so they work in every terminal and come back
by themselves after sleep, a network change or a reboot. You normally never run it directly:
`clipboard/install.sh` and `voice/install.sh` call it with their port.

```bash
./install.sh mybox 18377 4713     # ports accumulate in ~/.config/devbox-kit/tunnels/mybox.ports
```

| Machine you sit at | Service |
|---|---|
| Linux | systemd user unit `ssh-tunnels@mybox.service` (verified) |
| macOS | launchd agent `~/Library/LaunchAgents/io.github.devbox-kit.tunnels.mybox.plist` (written, not yet run on a Mac) |

Needs key login with no prompt: `ssh -o BatchMode=yes mybox true` must succeed (on a Mac the
key must be in the agent or have no passphrase, because the service has no terminal to ask in).

**Why a service and not `RemoteForward` in `~/.ssh/config`.** After a sleep or a reboot the box
keeps the old connection's ports until its sshd notices the connection is dead (often about
90 s). A terminal reconnecting in that window gets no tunnel and says nothing: copy and voice
just stop. The service runs `ssh -N -o ExitOnForwardFailure=yes`, so a refused port ends the
connection, and systemd or launchd restarts it every 10 s until the ports are free. The
installer also REMOVES any `RemoteForward` for these ports from the host's block in
`~/.ssh/config`, because an interactive session holding a port would keep the service out.

The installer believes the tunnel only when the service's connection has stayed up 10 s AND
the box is seen listening on every port over a separate connection.

## When it does not work

- Linux: `systemctl --user status ssh-tunnels@mybox`, `journalctl --user -u ssh-tunnels@mybox -n 20`
- macOS: `launchctl print gui/$(id -u)/io.github.devbox-kit.tunnels.mybox`,
  `~/Library/Logs/devbox-kit-tunnels-mybox.log`
- On the box: `ss -ltn | grep -E ':(18377|4713) '` lists the ports while the tunnel is up.
- `remote port forwarding failed` in the log: an older session on the box still holds the
  port. It frees itself; or end the old ssh session.
