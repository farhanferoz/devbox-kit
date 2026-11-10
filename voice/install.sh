#!/usr/bin/env bash
# ==============================================================================
# voice/install.sh - Microphone for Claude Code /voice over SSH
# ==============================================================================
# Run on the machine you SIT AT (the one with the microphone), Linux or macOS:
#   ./install.sh [ssh-host]          (defaults to $REMOTE_SESSION_HOST)
# The box must be Debian or Ubuntu (step 5 uses apt with sudo).
#
# The problem, measured on an NVIDIA DGX Spark 2026-08-29:
#   /voice enables, but recording never produces audio. Claude Code records by
#   shelling out to `rec` (SoX) or `arecord` (ALSA) -- confirmed in the 2.1.251
#   binary and in Anthropic's own docs -- and the DGX has no capture hardware:
#   /proc/asound/cards lists only the NVIDIA HDA controller, which is playback
#   only, and `arecord -l` reports zero capture devices. Anthropic states plainly
#   that voice dictation "does not work in remote environments such as ... SSH
#   sessions", because the microphone is on the local machine.
#
# The fix, and why it is the supported shape rather than a workaround:
#   Anthropic's own WSL guidance is this exact pattern -- WSLg has no ALSA capture
#   device either, so they tell you to route audio through PulseAudio and install
#   SoX's PulseAudio backend (`sudo apt install sox libsox-fmt-pulse`). We do the
#   same, with the PulseAudio server reached over an SSH reverse tunnel instead of
#   a local socket. Nothing about Claude Code is patched or bypassed: it records
#   with the same commands it always uses, and they now find a real microphone.
#
#   laptop (has the mic)                        DGX (headless)
#   ────────────────────                        ──────────────
#   pipewire-pulse                              claude /voice
#     listens 127.0.0.1:4713  ◄── RemoteForward ──┤ rec / arecord
#     (drop-in config)             4713           └─ ALSA default -> pulse
#                                                    PULSE_SERVER=tcp:127.0.0.1:4713
#
# Security -- read this before trusting it. The TCP listener is loopback-only on
# both ends and reachable only through your own outbound SSH session. That, and
# nothing else, is the control. MEASURED 2026-08-29: the PulseAudio cookie
# authenticates NOTHING here -- a client with no cookie at all reached the laptop
# and recorded the microphone -- and `client.access = "restricted"` made no
# difference, because an SSH reverse forward arrives from 127.0.0.1 and is
# classified local. The cookie is still copied because libpulse expects one to
# exist, NOT because it gates access. So: any local user on the remote host can
# use your microphone while a session is attached. Same trust model already
# accepted for the clipboard bridge, but do not mistake it for authentication.
#
# A genuinely authenticated variant exists and was tested: forward the unix
# socket instead (`RemoteForward /path/sock:/run/user/1000/pulse/native` with
# `StreamLocalBindMask 0177`), which yields a 0600 socket only your user can
# open. It is NOT used here because the DGX's sshd has `StreamLocalBindUnlink no`,
# so a stale socket from a previous session blocks every reconnection -- fixing
# that needs a system-wide sshd_config change. Revisit if that is acceptable.
# ==============================================================================

set -uo pipefail

REMOTE_HOST="${1:-${REMOTE_SESSION_HOST:-}}"
[ -n "$REMOTE_HOST" ] || { echo "usage: $0 <ssh-host>   (or set REMOTE_SESSION_HOST)" >&2; exit 2; }
PULSE_PORT=4713
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DROP_IN="${HOME}/.config/pipewire/pipewire-pulse.conf.d/50-tcp-for-ssh.conf"

serve_linux() {
echo ">>> 1. Checking this machine actually has a microphone to serve..."
command -v pactl >/dev/null || { echo "[ERROR] pactl not found; is PipeWire/PulseAudio installed?" >&2; exit 1; }
SOURCES="$(pactl list short sources 2>/dev/null | grep -c "alsa_input" || true)"
if [ "${SOURCES:-0}" -eq 0 ]; then
    echo "[ERROR] no ALSA capture source on this machine -- there is no microphone to forward." >&2
    exit 1
fi
echo "    ${SOURCES} capture source(s) present"

echo ">>> 2. Serving the PulseAudio protocol on 127.0.0.1:${PULSE_PORT}..."
# A drop-in, NOT a copy of /usr/share/pipewire/pipewire-pulse.conf: the man page
# recommends drop-ins, and a full copy silently goes stale when PipeWire updates.
mkdir -p "$(dirname "$DROP_IN")"
cat > "$DROP_IN" <<EOF
# Serve the PulseAudio protocol on loopback TCP as well as the usual unix socket, so an SSH
# reverse tunnel can carry this machine's microphone to a headless remote host.
#
# MEASURED 2026-08-29: the PulseAudio cookie does NOT gate this, and neither does
# \`client.access = "restricted"\` (tried, no effect). With HOME redirected and PULSE_COOKIE
# pointing at a nonexistent file, a remote client still reached this server and recorded the
# live microphone -- the SSH reverse forward makes the connection arrive from 127.0.0.1 and be
# classified local rather than network. The ONLY controls are that the port is loopback-only
# and reachable solely while an outbound SSH session is attached. Anyone with a shell on the
# remote host can use this microphone during that window.
pulse.properties = {
    server.address = [
        "unix:native"
        { address = "tcp:127.0.0.1:${PULSE_PORT}"
          max-clients = 8
          listen-backlog = 8
        }
    ]
}
EOF
# MEASURED 2026-08-29: pipewire-pulse binds the TCP port ITSELF; systemd's socket unit holds
# only the unix socket. So with the service merely socket-activated, stopping it removed 4713
# and a TCP connect did NOT bring it back -- "Connection refused". A configured RemoteForward
# with no listener makes ssh print "connect_to ... failed" into its own terminal, which
# corrupts a full-screen program like Claude Code. Enabling the service pins it to login, so
# the port exists for the whole session rather than only after something else touches audio.
systemctl --user enable pipewire-pulse.service >/dev/null 2>&1
systemctl --user restart pipewire-pulse
# Poll to a deadline rather than `sleep 1` then look once: the bind is asynchronous, so a
# fixed sleep is a race that fails on a loaded machine and wastes a second on an idle one.
listening=""
for _ in $(seq 1 25); do
    if ss -ltn 2>/dev/null | grep -q "127.0.0.1:${PULSE_PORT}"; then listening=1; break; fi
    sleep 0.2
done
if [ -z "$listening" ]; then
    echo "[ERROR] pipewire-pulse is not listening on ${PULSE_PORT}. Check: systemctl --user status pipewire-pulse" >&2
    exit 1
fi
echo "    listening on 127.0.0.1:${PULSE_PORT}"
}

# macOS: there is no PipeWire, so a PulseAudio server from Homebrew serves the microphone.
# UNTESTED (no Mac was available when this was written). Assumptions, each checked by the
# recording test in step 7 rather than trusted: Homebrew's pulseaudio reads ~/.config/pulse/default.pa
# in place of its own default.pa (so ours includes the stock one first); module-coreaudio-detect
# exposes the built-in microphone as a source; and macOS asks for microphone permission for the
# pulseaudio process. If permission is refused, the box records SILENCE, which step 7 reports.
serve_macos() {
echo ">>> 1. Checking for a PulseAudio server (Homebrew)..."
command -v brew >/dev/null || { echo "[ERROR] Homebrew is required on macOS: https://brew.sh" >&2; exit 1; }
command -v pulseaudio >/dev/null || brew install pulseaudio || { echo "[ERROR] brew install pulseaudio failed" >&2; exit 1; }
local stock; stock="$(brew --prefix)/etc/pulse/default.pa"
[ -r "$stock" ] || { echo "[ERROR] no stock config at ${stock}; is pulseaudio installed by Homebrew?" >&2; exit 1; }
echo ">>> 2. Serving the PulseAudio protocol on 127.0.0.1:${PULSE_PORT}..."
mkdir -p "${HOME}/.config/pulse"
cat > "${HOME}/.config/pulse/default.pa" <<EOF
# devbox-kit voice bridge: the stock config, plus the PulseAudio protocol on loopback TCP so an
# SSH reverse tunnel can carry this Mac's microphone to the box. auth-anonymous: the cookie would
# not protect this anyway (see voice/README.md, Security); loopback plus your own tunnel is the control.
.include ${stock}
load-module module-native-protocol-tcp listen=127.0.0.1 port=${PULSE_PORT} auth-anonymous=1
EOF
brew services restart pulseaudio >/dev/null 2>&1 || brew services start pulseaudio >/dev/null 2>&1
local listening=""
for _ in $(seq 1 50); do
    if nc -z 127.0.0.1 "${PULSE_PORT}" 2>/dev/null; then listening=1; break; fi
    sleep 0.2
done
[ -n "$listening" ] || { echo "[ERROR] pulseaudio is not listening on ${PULSE_PORT}. Check: brew services list; pulseaudio -vvv" >&2; exit 1; }
echo "    listening on 127.0.0.1:${PULSE_PORT}"
pactl list short sources 2>/dev/null | grep -v monitor | sed 's/^/    source: /' || true
}

case "$(uname -s)" in
    Linux)  serve_linux ;;
    Darwin) serve_macos ;;
    *) echo "[ERROR] $(uname -s) is not supported (Linux and macOS only)." >&2; exit 1 ;;
esac

echo ">>> 3. Carrying port ${PULSE_PORT} to ${REMOTE_HOST} (../tunnels, one always-on connection)..."
bash "${HERE}/../tunnels/install.sh" "$REMOTE_HOST" "$PULSE_PORT" \
    || echo "[WARN] the tunnel service is installed but port ${PULSE_PORT} is not listening on ${REMOTE_HOST} yet (see above)." >&2

echo ">>> 4. Copying the PulseAudio cookie to ${REMOTE_HOST} (piped, never printed)..."
COOKIE="${HOME}/.config/pulse/cookie"
# macOS: the server above accepts anonymous clients, so the cookie's content never matters there;
# the box's libpulse only needs one to exist. Create one if this Mac has none.
if [ "$(uname -s)" = Darwin ] && ! [ -r "$COOKIE" ]; then
    ( umask 077; mkdir -p "$(dirname "$COOKIE")"; head -c 256 /dev/urandom > "$COOKIE" )
fi
# `set -uo pipefail` has no -e, so a failed redirect would skip ssh entirely and the success
# line below would still print. Check the input, and check the ssh.
[ -r "$COOKIE" ] || { echo "[ERROR] no PulseAudio cookie at ${COOKIE} -- start an audio client once, then re-run." >&2; exit 1; }
if ! ssh "$REMOTE_HOST" 'umask 077; mkdir -p ~/.config/pulse; cat > ~/.config/pulse/cookie; chmod 600 ~/.config/pulse/cookie' < "$COOKIE"; then
    echo "[ERROR] copying the cookie to ${REMOTE_HOST} failed; voice will not authenticate." >&2
    exit 1
fi
echo "    cookie installed at mode 600"

echo ">>> 5. Installing the recorders and the PulseAudio ALSA plugin on ${REMOTE_HOST}..."
# libsox-fmt-pulse is the piece Anthropic's WSL note calls out: plain `sox` pulls the ALSA
# backend only, which cannot record where there is no /dev/snd capture device.
if ! ssh "$REMOTE_HOST" 'sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y sox libsox-fmt-pulse libasound2-plugins pulseaudio-utils >/dev/null 2>&1
missing=""
for t in rec arecord pactl; do command -v $t >/dev/null || missing="$missing $t"; done
if [ -n "$missing" ]; then echo "    [ERROR] still missing:$missing" >&2; exit 1; fi
echo "    rec, arecord, pactl present"'; then
    echo "[ERROR] the remote is missing a recorder; voice cannot work. Check apt on ${REMOTE_HOST}." >&2
    exit 1
fi

echo ">>> 6. Pointing the remote's ALSA and PulseAudio clients at the tunnel..."
# The address goes in config files libpulse reads UNCONDITIONALLY, not in an environment
# variable. MEASURED 2026-08-29: an export appended to ~/.bashrc lands behind Ubuntu's
# non-interactive early return, so a login shell, a script or a systemd user unit saw no
# server -- and because the remote runs its own pipewire-pulse whose default source is
# `auto_null.monitor`, arecord then returned 32000 bytes of DIGITAL SILENCE with exit 0.
# A dead tunnel fails loudly; an absent PULSE_SERVER failed silently, which is worse.
if ! ssh "$REMOTE_HOST" 'set -e
umask 022; mkdir -p ~/.config/pulse
[ -f ~/.asoundrc ] && ! [ -f ~/.asoundrc.before-voice-bridge ] && cp ~/.asoundrc ~/.asoundrc.before-voice-bridge
cat > ~/.config/pulse/client.conf <<CONF
# The microphone is on the machine you sit at, reached over the SSH reverse tunnel.
# Deliberately NOT PULSE_SERVER in ~/.bashrc: that file returns early for non-interactive
# shells, so anything not started from an interactive prompt silently recorded this host own
# auto_null.monitor instead. libpulse reads this file whatever started the process.
default-server = tcp:127.0.0.1:'"${PULSE_PORT}"'
CONF
cat > ~/.asoundrc <<CONF
# Route ALSA through PulseAudio, which the SSH reverse tunnel carries to the laptop.
# The server is named explicitly for the same reason as client.conf -- never depend on an
# environment variable that only interactive shells receive.
pcm.!default { type pulse server "tcp:127.0.0.1:'"${PULSE_PORT}"'" }
ctl.!default { type pulse server "tcp:127.0.0.1:'"${PULSE_PORT}"'" }
CONF
echo "    ~/.config/pulse/client.conf and ~/.asoundrc written (any prior .asoundrc saved)"'; then
    echo "[ERROR] could not write the audio client config on ${REMOTE_HOST}." >&2
    exit 1
fi

echo ">>> 7. Proving it: recording 1 second on ${REMOTE_HOST} from this machine's microphone..."
# Deliberately does NOT set PULSE_SERVER: setting it here exercises a path the user's real
# session may not have, and that is exactly how this check passed while /voice recorded
# silence. The check must look at the SAMPLES too -- a dead capture path returns a
# correctly-sized buffer of zeroes with every exit status still 0.
if ! ssh "$REMOTE_HOST" 'unset PULSE_SERVER
timeout -k 3 8 arecord -f S16_LE -r 16000 -c 1 -t raw -d 1 /tmp/vb-test.raw >/dev/null 2>&1
python3 - <<CHECK
import struct, pathlib
p = pathlib.Path("/tmp/vb-test.raw")
b = p.read_bytes() if p.exists() else b""
if not b:
    print("    [ERROR] nothing recorded"); raise SystemExit(1)
s = struct.unpack("<%dh" % (len(b)//2), b[:len(b)//2*2])
peak = max(abs(x) for x in s)
if peak <= 32:
    print(f"    [ERROR] {len(b)} bytes but peak amplitude {peak} -- DIGITAL SILENCE.")
    print("            The tunnel is not carrying audio, or the microphone is muted.")
    raise SystemExit(1)
print(f"    {len(b)} bytes, peak amplitude {peak} -> REAL AUDIO")
CHECK
status=$?
rm -f /tmp/vb-test.raw
exit $status'; then
    echo "[ERROR] ${REMOTE_HOST} could not record from this machine microphone." >&2
    echo "        Voice will NOT work. Do not rely on it until this passes." >&2
    exit 1
fi

echo
echo "Done. Open a FRESH ssh session to ${REMOTE_HOST}, then run /voice in Claude Code."
echo
echo "SECURITY: there is no authentication. While the tunnel is up, ANY user with a shell on"
echo "${REMOTE_HOST} can record this microphone. Use it only where you trust every account (voice/README.md)."
