#!/usr/bin/env bash
# ==============================================================================
# clipboard/install.sh - Copy/paste and image paste for Claude Code & agy over SSH
# ==============================================================================
# Run on the machine you SIT AT (Linux or macOS), once per remote box:
#   ./install.sh [ssh-host] [--selftest | --verify]
# ssh-host defaults to $REMOTE_SESSION_HOST (session/README.md, step 3).
#
# Linux: systemd socket activation holds the port and starts the bridge on demand,
# so nothing runs while idle. macOS: a launchd agent keeps the bridge running.
# Either way ../tunnels carries the port to the box on one always-on connection.
#
# Fixes three faults on the path from your machine to a headless box:
#   1. Claude Code grabs the mouse, so drag-select stops reaching the terminal
#                              -> hold Shift; do NOT set CLAUDE_CODE_DISABLE_MOUSE
#   2. Its "copy" picks a tool only when DISPLAY/WAYLAND_DISPLAY is set, and
#      otherwise falls back to OSC 52, which VTE swallows
#                              -> set WAYLAND_DISPLAY on the remote, bridged wl-copy
#   3. Image paste shells out to wl-paste/xclip, impossible headless
#                              -> bridged wl-paste/xclip
# ==============================================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=18377
TOKEN_DIR="${HOME}/.config/clipboard-bridge"
TOKEN_FILE="${TOKEN_DIR}/token"
UNIT_DIR="${HOME}/.config/systemd/user"
# The bridge runs from a SNAPSHOT here, never from the repo: a `git pull`, a move or a sync
# tool rewriting the checkout must not change or break a running clipboard. Re-run the
# installer to take a new version.
LIB_DIR="${HOME}/.local/lib/clipboard-bridge"

REMOTE_HOST="${REMOTE_SESSION_HOST:-}"
SELFTEST="no"
VERIFY="no"
for arg in "$@"; do
    case "$arg" in
        --selftest) SELFTEST="yes" ;;
        --verify)   VERIFY="yes" ;;
        -*) echo "[ERROR] unknown option: $arg" >&2; exit 2 ;;
        *) REMOTE_HOST="$arg" ;;
    esac
done
[ -n "$REMOTE_HOST" ] || { echo "usage: $0 <ssh-host> [--selftest | --verify]   (or set REMOTE_SESSION_HOST)" >&2; exit 2; }

# StrEnum is used by both clipboard programs. Prefer a suitable interpreter on
# PATH, then Homebrew; Apple's bundled Python 3.9 cannot run the bridge.
PYTHON_BIN=""
for candidate in "$(command -v python3 || true)" /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -n "$candidate" ] && [ -x "$candidate" ] && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' 2>/dev/null; then
        PYTHON_BIN="$candidate"
        break
    fi
done
[ -n "$PYTHON_BIN" ] || { echo "[ERROR] Python 3.11 or newer is required (macOS: brew install python)." >&2; exit 1; }
export PATH="$(dirname "$PYTHON_BIN"):$PATH"

# macOS has no sha256sum and no `readlink -f`; keep every step portable.
hash_of() { python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest()[:12])' "$1"; }
# render SED_SCRIPT TEMPLATE OUTPUT: fill a template and refuse to install one with a
# placeholder left over (a unit with a literal @HOST@ in it fails only when it fires).
render() {
    sed "$1" "$2" > "$3"
    if grep -qE '@[A-Z_]+@' "$3"; then echo "[ERROR] unfilled placeholder in $3: $(grep -oE '@[A-Z_]+@' "$3" | sort -u | tr '\n' ' ')" >&2; exit 1; fi
}

# Reads both selections, proves a remote round trip on each, and puts them back. Non-destructive
# so it is safe to run unattended from the daily updater. Returns 1 on any failure.
roundtrip_checks() {
    local backend="$1" host="$2" rc=0
    if [ "$backend" = "macos" ]; then
        # pbpaste/pbcopy can restore only text and would replace a copied Finder
        # file or screenshot. The native test preserves every pasteboard item.
        xcrun swift "${REPO_DIR}/tests/test_macos_clipboard.swift" "$host"
        return $?
    fi
    local saved_prim probe pprobe got pgot xgot wprobe werr still perr clipfile clipmime
    perr=$(mktemp); clipfile=$(mktemp)
    saved_prim=""; clipmime=""
    # The clipboard is saved as BYTES through a file, not through a shell variable: command
    # substitution strips NUL bytes, so a copied screenshot came back as mangled text. This
    # runs daily and unattended, so silently corrupting an image is worse than not running.
    # PRIMARY is a mouse text selection and has no binary case.
    case "$backend" in
        wayland) clipmime=$(wl-paste --list-types 2>/dev/null | head -1 || true)
                 [ -n "$clipmime" ] && wl-paste --type "$clipmime" > "$clipfile" 2>/dev/null
                 saved_prim=$(wl-paste --primary --no-newline 2>/dev/null || true) ;;
        x11)     clipmime=$(xclip -selection clipboard -t TARGETS -o 2>/dev/null | grep -m1 "/" || true)
                 [ -n "$clipmime" ] && xclip -selection clipboard -t "$clipmime" -o > "$clipfile" 2>/dev/null
                 saved_prim=$(xclip -selection primary -o 2>/dev/null || true) ;;
        macos)   pbpaste > "$clipfile" 2>/dev/null || true ;;
    esac

    probe="clipboard-bridge-$(date +%s)"
    pprobe="clipboard-bridge-primary-$(date +%s)"
    case "$backend" in
        wayland) printf '%s' "$probe"  | wl-copy --type text/plain >/dev/null 2>&1
                 printf '%s' "$pprobe" | wl-copy --primary --type text/plain >/dev/null 2>&1 ;;
        x11)     printf '%s' "$probe"  | xclip -selection clipboard >/dev/null 2>&1
                 printf '%s' "$pprobe" | xclip -selection primary   >/dev/null 2>&1 ;;
        macos)   printf '%s' "$probe"  | pbcopy ;;
    esac

    # shellcheck disable=SC2016  # $HOME/$PATH must expand on the remote host.
    got=$(ssh "$host" 'export PATH=$HOME/.local/bin:$PATH; wl-paste --type text/plain' 2>"$perr" || true)
    if [ "$got" = "$probe" ]; then echo "    PASS - CLIPBOARD round trip (Ctrl+V)"
    else echo "    FAIL - CLIPBOARD: expected '${probe}', got '${got}' [$(tr '\n' ' ' < "$perr")]" >&2; rc=1; fi

    if [ "$backend" != "macos" ]; then
        # The exact chain Claude Code shells out to for a middle click. Drive it, not an
        # equivalent: the point is to break when Claude Code's chain changes.
        # shellcheck disable=SC2016
        pgot=$(ssh "$host" 'export PATH=$HOME/.local/bin:$PATH; wl-paste --primary --no-newline' 2>"$perr" || true)
        if [ "$pgot" = "$pprobe" ]; then echo "    PASS - PRIMARY round trip (middle-click), wl-paste"
        else echo "    FAIL - wl-paste --primary: expected '${pprobe}', got '${pgot}' [$(tr '\n' ' ' < "$perr")]" >&2; rc=1; fi

        # shellcheck disable=SC2016
        xgot=$(ssh "$host" 'export PATH=$HOME/.local/bin:$PATH; xclip -selection primary -o' 2>"$perr" || true)
        if [ "$xgot" = "$pprobe" ]; then echo "    PASS - PRIMARY round trip (middle-click), xclip"
        else echo "    FAIL - xclip -selection primary: expected '${pprobe}', got '${xgot}' [$(tr '\n' ' ' < "$perr")]" >&2; rc=1; fi

        # A drag-select inside a remote Claude Code WRITES the mouse selection back to this
        # machine. That is the half that was refused for two days, so middle-click pasted
        # stale text while Ctrl+Shift+V looked fine. Prove the write lands.
        # `export`, not a `VAR=val cmd` prefix: the prefix binds to the FIRST command only, so
        # `PATH=... printf x | wl-copy` runs the real /usr/bin/wl-copy instead of the shim.
        wprobe="clipboard-bridge-writeback-$(date +%s)"
        # shellcheck disable=SC2016
        werr=$(ssh "$host" "export PATH=\$HOME/.local/bin:\$PATH; printf %s '${wprobe}' | wl-copy --primary" 2>&1 >/dev/null || true)
        sleep 1
        case "$backend" in
            wayland) still=$(wl-paste --primary --no-newline 2>/dev/null || true) ;;
            x11)     still=$(xclip -selection primary -o 2>/dev/null || true) ;;
        esac
        if [ "$still" = "$wprobe" ]; then
            echo "    PASS - remote drag-select writes back to your mouse selection"
        else
            echo "    FAIL - write-back: expected '${wprobe}', selection holds '${still}' [${werr}]" >&2; rc=1
        fi
    fi

    # Put both selections back the way they were.
    case "$backend" in
        wayland) if [ -s "$clipfile" ]; then wl-copy --type "$clipmime" < "$clipfile" >/dev/null 2>&1; else wl-copy --clear >/dev/null 2>&1 || true; fi
                 if [ -n "$saved_prim" ]; then printf '%s' "$saved_prim" | wl-copy --primary --type text/plain >/dev/null 2>&1; else wl-copy --primary --clear >/dev/null 2>&1 || true; fi ;;
        x11)     if [ -s "$clipfile" ]; then xclip -selection clipboard -t "$clipmime" -i "$clipfile" >/dev/null 2>&1; fi
                 printf '%s' "$saved_prim" | xclip -selection primary >/dev/null 2>&1 ;;
        macos)   pbcopy < "$clipfile" ;;
    esac
    rm -f "$perr" "$clipfile"
    return $rc
}

# Both ends must be the same build: the shim on the box is a snapshot copy, so it does not
# update itself, and nothing else would report the two ends drifting apart.
drift_check() {
    local host="$1" rc=0 want_shim have_shim want_bridge have_bridge
    want_shim=$(hash_of "${REPO_DIR}/clip_shim.py")
    have_shim=$(ssh "$host" 'python3 -c "import hashlib,pathlib,sys; p=pathlib.Path.home()/\".local/bin/clip-shim\"; print(hashlib.sha256(p.read_bytes()).hexdigest()[:12] if p.exists() else \"MISSING\")"' 2>/dev/null || echo UNREACHABLE)
    if [ "$want_shim" = "$have_shim" ]; then echo "    PASS - remote shim matches this repo (${want_shim})"
    else echo "    FAIL - remote shim is ${have_shim}, repo is ${want_shim}: re-run without --verify" >&2; rc=1; fi

    want_bridge=$(hash_of "${REPO_DIR}/clipboard_bridge.py")
    if [ -f "${LIB_DIR}/clipboard_bridge.py" ]; then have_bridge=$(hash_of "${LIB_DIR}/clipboard_bridge.py"); else have_bridge=MISSING; fi
    if [ "$want_bridge" = "$have_bridge" ]; then echo "    PASS - installed bridge matches this repo (${want_bridge})"
    else echo "    FAIL - installed bridge is ${have_bridge}, repo is ${want_bridge}: re-run without --verify" >&2; rc=1; fi
    return $rc
}

if [ "$VERIFY" = "yes" ]; then
    echo ">>> Verifying the clipboard bridge for ${REMOTE_HOST} (nothing is installed or overwritten)..."
    BACKEND="$("${REPO_DIR}/clipboard_bridge.py" --backend)"
    vrc=0
    drift_check "$REMOTE_HOST" || vrc=1
    roundtrip_checks "$BACKEND" "$REMOTE_HOST" || vrc=1
    if [ "$vrc" -ne 0 ]; then
        echo "[ERROR] clipboard bridge verification FAILED for ${REMOTE_HOST}." >&2
        exit 1
    fi
    echo "    all checks passed."
    exit 0
fi

echo ">>> 1. Checking this machine has a clipboard we can serve..."
command -v python3 >/dev/null || { echo "[ERROR] python3 is required (on macOS: xcode-select --install)" >&2; exit 1; }
BACKEND="$("${REPO_DIR}/clipboard_bridge.py" --backend)"
echo "    backend: ${BACKEND}"
if [ "$BACKEND" = "macos" ]; then
    command -v xcrun >/dev/null && xcrun --find swiftc >/dev/null 2>&1 \
        || { echo "[ERROR] Apple Command Line Tools are required: xcode-select --install" >&2; exit 1; }
    [ -f "${REPO_DIR}/macos_clipboard.swift" ] \
        || { echo "[ERROR] missing macos_clipboard.swift; update the complete repo before installing." >&2; exit 1; }
fi

echo ">>> 2. Generating the shared token (never printed)..."
if [ -s "$TOKEN_FILE" ]; then
    echo "    reusing existing token"
else
    mkdir -p "$TOKEN_DIR"
    ( umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE" )
    echo "    created ${TOKEN_FILE}"
fi
chmod 600 "$TOKEN_FILE"
echo "    fingerprint: $(hash_of "$TOKEN_FILE")"

echo ">>> 3. Installing the bridge service on this machine..."
if [ "$BACKEND" = "macos" ]; then
    # launchd keeps the bridge running (KeepAlive); it is idle until the box asks. launchd's
    # own PATH lacks Homebrew, where pngpaste (screenshot paste) lives, so it is set here.
    LABEL="io.github.devbox-kit.clipboard-bridge"
    PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
    mkdir -p "$LIB_DIR" "$(dirname "$PLIST")" "${HOME}/Library/Logs"
    # Build before replacing the installed bridge so a compiler failure leaves
    # the existing service snapshot intact.
    xcrun swiftc -O "${REPO_DIR}/macos_clipboard.swift" -o "${LIB_DIR}/macos-clipboard-helper.new"
    mv "${LIB_DIR}/macos-clipboard-helper.new" "${LIB_DIR}/macos-clipboard-helper"
    cp "${REPO_DIR}/clipboard_bridge.py" "${LIB_DIR}/clipboard_bridge.py"
    chmod 755 "${LIB_DIR}/clipboard_bridge.py"
    render "s#@LABEL@#${LABEL}#g; s#@PYTHON@#$(command -v python3)#g; s#@BRIDGE_DIR@#${LIB_DIR}#g; s#@LOG@#${HOME}/Library/Logs/devbox-kit-clipboard-bridge.log#g" \
        "${REPO_DIR}/clipboard-bridge.plist.in" "$PLIST"
    plutil -lint "$PLIST" >/dev/null || { echo "[ERROR] generated an invalid plist: $PLIST" >&2; exit 1; }
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    for _ in $(seq 1 25); do "${LIB_DIR}/clipboard_bridge.py" --check && break; sleep 0.2; done
    "${LIB_DIR}/clipboard_bridge.py" --check || { echo "[ERROR] the bridge is not listening on 127.0.0.1:${PORT}; see ~/Library/Logs/devbox-kit-clipboard-bridge.log" >&2; exit 1; }
    echo "    bridge running under launchd (${LABEL}), snapshot $(hash_of "${LIB_DIR}/clipboard_bridge.py")"
    command -v pngpaste >/dev/null || echo "    note: 'brew install pngpaste' makes screenshot paste faster (AppleScript is used otherwise)"
elif ! command -v systemctl >/dev/null 2>&1; then
    echo "[ERROR] no systemd user manager on this Linux machine; run the bridge by hand: ${REPO_DIR}/clipboard_bridge.py" >&2
    exit 1
else
    # Socket activation: systemd owns the port, so a listener is always there, and nothing
    # runs while idle (the bridge exits after five minutes without a request).
    mkdir -p "$UNIT_DIR" "$LIB_DIR"
    cp "${REPO_DIR}/clipboard_bridge.py" "${LIB_DIR}/clipboard_bridge.py"
    cp "${REPO_DIR}/README.md"           "${LIB_DIR}/README.md"
    chmod 755 "${LIB_DIR}/clipboard_bridge.py"
    render "s#@BRIDGE_DIR@#${LIB_DIR}#g; s#@PORT@#${PORT}#g" "${REPO_DIR}/clipboard-bridge.socket.in" "${UNIT_DIR}/clipboard-bridge.socket"
    render "s#@BRIDGE_DIR@#${LIB_DIR}#g; s#@PORT@#${PORT}#g" "${REPO_DIR}/clipboard-bridge.service.in" "${UNIT_DIR}/clipboard-bridge.service"
    systemctl --user daemon-reload
    systemctl --user enable --now clipboard-bridge.socket >/dev/null 2>&1
    # Retire any instance still running the old ExecStart; the socket keeps listening, so the
    # next request starts a fresh one and no "connect_to ... failed" reaches a TUI.
    systemctl --user stop clipboard-bridge.service >/dev/null 2>&1 || true
    echo "    bridge snapshot: ${LIB_DIR}/clipboard_bridge.py ($(hash_of "${LIB_DIR}/clipboard_bridge.py"))"
    # A daily check, because nothing else would notice a Claude Code update changing the
    # commands it shells out to for a paste.
    render "s#@REPO_DIR@#${REPO_DIR}#g; s#@BRIDGE_DIR@#${LIB_DIR}#g; s#@HOST@#${REMOTE_HOST}#g" \
        "${REPO_DIR}/clipboard-bridge-verify.service.in" "${UNIT_DIR}/clipboard-bridge-verify.service"
    cp "${REPO_DIR}/clipboard-bridge-verify.timer.in" "${UNIT_DIR}/clipboard-bridge-verify.timer"
    systemctl --user daemon-reload
    systemctl --user enable --now clipboard-bridge-verify.timer >/dev/null 2>&1
    echo "    daily check: $(systemctl --user is-active clipboard-bridge-verify.timer) (next $(systemctl --user show clipboard-bridge-verify.timer -p NextElapseUSecRealtime --value))"
    echo "    socket: $(systemctl --user is-active clipboard-bridge.socket)   service: $(systemctl --user is-active clipboard-bridge.service) (idle is correct)"
fi

echo ">>> 4. Carrying port ${PORT} to ${REMOTE_HOST} (../tunnels, one always-on connection)..."
# Not a RemoteForward in ~/.ssh/config: a terminal that reconnects soon after a sleep finds
# the box still holding the old session's port, gets no tunnel, and says nothing.
bash "${REPO_DIR}/../tunnels/install.sh" "$REMOTE_HOST" "$PORT" \
    || echo "[WARN] the tunnel service is installed but port ${PORT} is not listening on ${REMOTE_HOST} yet (see above)." >&2

echo ">>> 5. Copying the token to ${REMOTE_HOST} (piped, never printed)..."
ssh "$REMOTE_HOST" 'umask 077; mkdir -p ~/.config/clipboard-bridge; cat > ~/.config/clipboard-bridge/token; chmod 600 ~/.config/clipboard-bridge/token' < "$TOKEN_FILE"
ssh "$REMOTE_HOST" 'python3 - <<PY
import hashlib, pathlib
print(hashlib.sha256((pathlib.Path.home() / ".config/clipboard-bridge/token").read_bytes()).hexdigest()[:12])
PY' | sed 's/^/    remote fingerprint: /'

echo ">>> 6. Installing the shims on ${REMOTE_HOST}..."
ssh "$REMOTE_HOST" 'mkdir -p ~/.local/bin; cat > ~/.local/bin/clip-shim; chmod 755 ~/.local/bin/clip-shim' < "${REPO_DIR}/clip_shim.py"
ssh "$REMOTE_HOST" 'cd ~/.local/bin && for name in wl-paste wl-copy xclip; do ln -sf clip-shim "$name"; done && echo "    wl-paste, wl-copy, xclip -> clip-shim"'

echo ">>> 7. Making sure the mouse is NOT disabled on ${REMOTE_HOST}..."
# Measured, and the reason this step now REMOVES rather than adds: Claude Code emits only
# DECSET 1000/1006 (click + SGR), never 1002/1003 (motion), and it has its own wheelUp /
# wheelDown handlers. So with mouse reporting ON it scrolls correctly; with
# CLAUDE_CODE_DISABLE_MOUSE=1 the terminal turns the wheel into arrow keys and Claude Code
# warns "Scroll wheel is sending arrow keys". Selection does not need the variable either:
# holding Shift hands the mouse back to VTE, which is what Claude Code's own help text says.
ssh "$REMOTE_HOST" 'python3 - <<PY
import pathlib, shutil
rc = pathlib.Path.home() / ".bashrc"
if not rc.exists():
    print("    no ~/.bashrc")
    raise SystemExit(0)
lines = rc.read_text().splitlines()
# Drop the export ONLY when it is a line of its own: deleting every line that merely
# mentions the name could take unrelated shell logic with it. Its explanatory comments
# are dropped too, so no orphan is left pointing at a variable that is gone.
drop = ("CLAUDE_CODE_DISABLE_MOUSE", "Claude Code grabs the mouse", "owning drag-select and middle-click")
kept, removed = [], 0
for line in lines:
    bare = line.strip()
    if any(m in line for m in drop) and (bare.startswith("export CLAUDE_CODE_DISABLE_MOUSE") or bare.startswith("#")):
        removed += 1
        continue
    if "CLAUDE_CODE_DISABLE_MOUSE" in line:
        print(f"    WARNING: left a compound line alone: {bare[:60]}")
    kept.append(line)
if removed:
    shutil.copy2(rc, rc.with_suffix(".bak-clipboard-bridge"))
    rc.write_text("\n".join(kept) + "\n")
print(f"    removed {removed} mouse-disable line(s)" if removed else "    clean - mouse reporting left on; hold Shift to select")
PY'


echo ">>> 8. Letting the remote pick a clipboard tool (fault 2)..."
# Measured in the claude 2.1.247 bundle: the tool probe is gated on the environment --
#   function z(){return{display:process.env.DISPLAY,waylandDisplay:process.env.WAYLAND_DISPLAY}}
#   ... case "linux": if (typeof S().tool === "string") return "native"; break
#   ... return "osc52"
# With both unset on a headless host no tool is chosen, so every copy silently takes the
# OSC 52 path, which VTE does not implement. Naming a Wayland display -- there is no
# compositor, and the shim does not need one -- makes it choose our bridged wl-copy.
ssh "$REMOTE_HOST" 'grep -q "WAYLAND_DISPLAY=clipboard-bridge" ~/.bashrc 2>/dev/null || printf "\n# Headless, so nothing here opens a compositor. This only makes Claude Code and agy\n# choose a clipboard tool at all; without it they fall back to OSC 52, which VTE drops.\nexport WAYLAND_DISPLAY=clipboard-bridge\n" >> ~/.bashrc; echo "    WAYLAND_DISPLAY=clipboard-bridge present in ~/.bashrc"'

if [ "$SELFTEST" = "yes" ]; then
    echo ">>> 9. End-to-end self-test (both selections are restored afterwards)..."
    strc=0
    drift_check "$REMOTE_HOST" || strc=1
    roundtrip_checks "$BACKEND" "$REMOTE_HOST" || strc=1
    [ "$strc" -eq 0 ] || { echo "[ERROR] self-test FAILED." >&2; exit 1; }
fi

cat <<NEXT
========================================================
Clipboard bridge installed for ${REMOTE_HOST} (${BACKEND}).

The bridge waits on this machine (Linux: systemd socket, macOS: launchd)
and one always-on tunnel carries it to ${REMOTE_HOST}, so every session
there has a clipboard.

Start a new session on ${REMOTE_HOST} so it picks up WAYLAND_DISPLAY.
Paste a screenshot with Ctrl+V in Claude Code. Hold Shift to select with
the terminal instead of Claude Code. On Linux, middle-click pastes what you
last highlighted; Ctrl+Shift+V pastes what you last copied.
Diagnose on the remote with: tail -20 ~/.cache/clipboard-bridge.log
========================================================
NEXT
