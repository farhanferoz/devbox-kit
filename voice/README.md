# Voice bridge: a microphone for Claude Code `/voice` over SSH

`/voice` in Claude Code records by running `rec` (SoX) or `arecord` (ALSA). On a headless box
there is no microphone, so it records nothing; Anthropic's docs say voice dictation does not
work over SSH for this reason. The bridge serves the microphone of the machine you sit at as a
PulseAudio server on `127.0.0.1:4713`, carries that port to the box with
[../tunnels](../tunnels/README.md), and points the box's ALSA and PulseAudio clients at it.
Claude Code is not changed: it records with the same commands and finds a real microphone.
This is the same shape Anthropic recommends for WSL (PulseAudio plus `libsox-fmt-pulse`).

## Security: read this first

**There is no authentication. While your tunnel is up, any user with a shell on the box can
record your microphone.** The PulseAudio cookie does not prevent it: a tunnelled connection
arrives from 127.0.0.1 and is treated as local (measured: a client with no cookie recorded the
live microphone; `client.access = "restricted"` changed nothing). The only controls are that the
port listens on loopback at both ends and exists only while your own tunnel is up. Use this only
on a box where every other account is someone you would let hear you.

An authenticated variant exists and was tested: forward the PulseAudio *unix socket* instead of
TCP, with `StreamLocalBindMask 0177`, which gives a socket only your user can open. It needs
`StreamLocalBindUnlink yes` in the box's `sshd_config` (otherwise a stale socket from an earlier
session blocks every reconnection), so it is not the default here.

## Install

Prerequisites: the box is Debian or Ubuntu, and your user can `sudo` without a password prompt
for `apt-get` (step 5), or install `sox libsox-fmt-pulse libasound2-plugins pulseaudio-utils`
on the box yourself first.

```bash
cd ~/dev/devbox-kit/voice
./install.sh mybox          # default: $REMOTE_SESSION_HOST
```

| Machine you sit at | What serves the microphone | Status |
|---|---|---|
| Linux (PipeWire) | `pipewire-pulse`, plus a drop-in adding the TCP listener | verified (Fedora to Ubuntu 24.04) |
| macOS | PulseAudio from Homebrew (`brew install pulseaudio`, `brew services`) | written, **not yet run on a Mac** |
| Windows | | not supported |

The last step records one second on the box and checks the samples, not just the byte count: a
broken path returns the right number of bytes, all zero, with every exit status 0. It prints
`REAL AUDIO` with the peak amplitude, or fails. Then open a new session on the box and run
`/voice` in Claude Code.

On macOS, expect a microphone-permission prompt the first time PulseAudio records; if it is
refused, the box records silence and the installer says so. Grant it in *System Settings →
Privacy & Security → Microphone*.

## What it changes

- Machine you sit at, Linux: `~/.config/pipewire/pipewire-pulse.conf.d/50-tcp-for-ssh.conf`, and
  `pipewire-pulse.service` enabled so the port exists for the whole login.
- Machine you sit at, macOS: `~/.config/pulse/default.pa` (the stock file included, plus the
  TCP listener), and the Homebrew service started.
- Box: `~/.config/pulse/cookie`, `~/.config/pulse/client.conf` and `~/.asoundrc` (a previous
  `~/.asoundrc` is kept as `~/.asoundrc.before-voice-bridge`). The address is written into these
  files, not into `PULSE_SERVER` in `~/.bashrc`: Ubuntu's `~/.bashrc` returns early for
  non-interactive shells, and a process that missed the variable recorded the box's own silent
  default source without any error.

## When it does not work

- On the box: `pactl info` should name the machine you sit at as the server host;
  `arecord -f S16_LE -r 16000 -c 1 -d 2 /tmp/t.wav` should record sound.
- `Unable to connect: Timeout`: the tunnel is down (see ../tunnels), or the microphone on the
  machine you sit at is itself not working; test it there first.
