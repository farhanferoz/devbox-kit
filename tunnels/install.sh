#!/usr/bin/env bash
# tunnels/install.sh <host> <port>... : carry loopback ports to <host> on ONE always-on SSH connection.
#
# Run on the machine you sit at (Linux or macOS). Called by clipboard/install.sh and voice/install.sh;
# safe to re-run, and ports accumulate (~/.config/devbox-kit/tunnels/<host>.ports).
#
# Why a service and not RemoteForward lines in ~/.ssh/config: a terminal that reconnects soon after a
# reboot or wake finds the box still holding the dead session's ports, gets no tunnel, and says
# nothing, so copy and voice just stop. A service quits when a forward fails and is restarted until
# the ports are free, and reconnects by itself after a wake or a network change.
#   Linux: a systemd user unit, ssh-tunnels@<host>.service
#   macOS: a launchd agent, ~/Library/LaunchAgents/io.github.devbox-kit.tunnels.<host>.plist
# Needs key login to <host> with no prompt (`ssh -o BatchMode=yes <host> true` must succeed).
set -uo pipefail

[ $# -ge 2 ] || { echo "usage: $0 <host> <port>..." >&2; exit 2; }
HOST="$1"; shift
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_DIR="${HOME}/.config/devbox-kit/tunnels"
SSH_CONFIG="${SSH_CONFIG:-${HOME}/.ssh/config}"
WAIT_S=120   # the box frees a dead session's ports once its sshd notices (ClientAlive), often 90 s

[[ "$HOST" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "[ERROR] not an ssh host alias: $HOST" >&2; exit 2; }
for p in "$@"; do [[ "$p" =~ ^[0-9]{1,5}$ ]] || { echo "[ERROR] not a port: $p" >&2; exit 2; }; done
ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" true 2>/dev/null \
    || { echo "[ERROR] 'ssh -o BatchMode=yes $HOST true' failed: set up key login to $HOST first (session/README.md, step 2)." >&2; exit 1; }

mkdir -p "$CONF_DIR"
PORTS_FILE="${CONF_DIR}/${HOST}.ports"
{ cat "$PORTS_FILE" 2>/dev/null; printf '%s\n' "$@"; } | grep -E '^[0-9]+$' | sort -un > "${PORTS_FILE}.tmp" && mv -f "${PORTS_FILE}.tmp" "$PORTS_FILE"
echo "    tunnels to ${HOST}: $(xargs < "$PORTS_FILE")"

# Interactive sessions must not hold these ports (a held port keeps the service out).
while read -r p; do
    python3 "${HERE}/ensure_ssh_remote_forward.py" --remove "$SSH_CONFIG" "$HOST" "$p" || exit 1
done < "$PORTS_FILE"

case "$(uname -s)" in
Linux)
    command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1 \
        || { echo "[ERROR] no systemd user manager here; run the tunnel by hand: ssh -N -R 127.0.0.1:P:127.0.0.1:P $HOST" >&2; exit 1; }
    UNIT_DIR="${HOME}/.config/systemd/user"; UNIT="ssh-tunnels@${HOST}.service"
    mkdir -p "$UNIT_DIR"
    forwards=""
    while read -r p; do forwards+="-R 127.0.0.1:${p}:127.0.0.1:${p} "; done < "$PORTS_FILE"
    printf 'FORWARDS="%s"\n' "${forwards% }" > "${CONF_DIR}/${HOST}.env"
    cp "${HERE}/ssh-tunnels@.service.in" "${UNIT_DIR}/ssh-tunnels@.service"
    systemctl --user daemon-reload
    systemctl --user enable "$UNIT" >/dev/null 2>&1 || { echo "[ERROR] could not enable ${UNIT}" >&2; exit 1; }
    systemctl --user restart "$UNIT"
    # Seconds the unit has run without a restart (0 when not running). systemd's stamp is CLOCK_MONOTONIC.
    up_for() {
        local state since
        state="$(systemctl --user show "$UNIT" -p SubState --value)"
        since="$(systemctl --user show "$UNIT" -p ActiveEnterTimestampMonotonic --value)"
        [ "$state" = running ] && [ "${since:-0}" -gt 0 ] || { echo 0; return; }
        echo $(( ($(python3 -I -c 'import time; print(int(time.monotonic() * 1e6))') - since) / 1000000 ))
    }
    logs="journalctl --user -u ${UNIT} -n 20"
    ;;
Darwin)
    LABEL="io.github.devbox-kit.tunnels.${HOST}"
    PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
    LOG="${HOME}/Library/Logs/devbox-kit-tunnels-${HOST}.log"
    mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
    forwards=""
    while read -r p; do forwards+="        <string>-R</string><string>127.0.0.1:${p}:127.0.0.1:${p}</string>"$'\n'; done < "$PORTS_FILE"
    python3 - "${HERE}/tunnels.plist.in" "$PLIST" "$LABEL" "$HOST" "$LOG" "${forwards%$'\n'}" <<'PY'
import sys, pathlib
src, dst, label, host, log, forwards = sys.argv[1:]
text = pathlib.Path(src).read_text()
for k, v in {"@LABEL@": label, "@HOST@": host, "@LOG@": log, "@FORWARDS@": forwards}.items():
    text = text.replace(k, v)
pathlib.Path(dst).write_text(text)
PY
    plutil -lint "$PLIST" >/dev/null || { echo "[ERROR] generated an invalid plist: $PLIST" >&2; exit 1; }
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST" || { echo "[ERROR] launchctl bootstrap failed for $PLIST" >&2; exit 1; }
    # launchd reports no start time, so "up" is: running now, and its pid unchanged over 10 s.
    up_for() {
        local a b
        a="$(launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | awk '/^\tpid = /{print $3}')"
        [ -n "$a" ] || { echo 0; return; }
        sleep 10
        b="$(launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | awk '/^\tpid = /{print $3}')"
        [ "$a" = "$b" ] && echo 10 || echo 0
    }
    logs="tail -20 ${LOG}"
    ;;
*) echo "[ERROR] $(uname -s) is not supported (Linux and macOS only)." >&2; exit 1 ;;
esac

# Proof is the SERVICE's connection staying up 10 s (ExitOnForwardFailure: up means every forward
# bound) AND the box listening, read over a fresh connection. Listening alone is not proof: an
# older interactive session can hold the ports while the service keeps failing.
missing=""
deadline=$((SECONDS + WAIT_S))
while [ "$SECONDS" -lt "$deadline" ]; do
    sleep 5
    [ "$(up_for)" -ge 10 ] || { missing="(the service has not held its connection for 10 s)"; continue; }
    missing=""
    listening="$(ssh -o BatchMode=yes -o ConnectTimeout=10 -o ClearAllForwardings=yes "$HOST" 'ss -ltn' 2>/dev/null)"
    while read -r p; do grep -q "127.0.0.1:${p} " <<<"$listening" || missing+="$p "; done < "$PORTS_FILE"
    [ -z "$missing" ] && { echo "    tunnel service up; ${HOST} listens on every tunnel port"; exit 0; }
done
echo "[WARN] after ${WAIT_S} s ${HOST} still does not listen on: ${missing}. An older interactive session may still" >&2
echo "       hold them; the service keeps retrying every 10 s. Reconnect old terminals, then: ${logs}" >&2
exit 1
