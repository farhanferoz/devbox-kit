#!/usr/bin/env python3
"""Stand in for `wl-paste`, `wl-copy` and `xclip` on a headless remote host.

Installed into `~/.local/bin` (which precedes `/usr/bin` on the DGX), symlinked under all
three names, and dispatching on `argv[0]`. Anything that reads or writes the clipboard by
shelling out -- Claude Code and `agy` both do, and neither will stop, since claude-code#42712
asked for the terminal-native route and was closed "not planned" -- transparently reaches the
laptop's clipboard instead of a display-server error.

The far side is `clipboard_bridge.py`, reached over the loopback port that `ssh -R` carries
into this host from `~/.ssh/config`. systemd socket activation holds that port on the laptop,
so any ssh session has it; nothing here depends on how the session was started.

Argument parsing is deliberately faithful rather than minimal. A review found that a partial
parse is worse than no support at all: `xclip -selection clipboard -t image/png -i shot.png`
(which is how Claude Code copies a screenshot) was falling through to a stdin read, sending a
zero-byte WRITE, and thereby WIPING the laptop clipboard while reporting success. Unrecognised
input is now refused, never silently reinterpreted as "copy nothing".

Failure is deliberately quiet and fast. When the tunnel is down -- no session attached, or the
laptop asleep -- callers must fall through to their next option rather than hang, so a short
connect deadline and a bare non-zero exit are the correct behaviour, not an error to surface.

Whose clipboard? The forwarded port belongs to whichever laptop session bound it first, so a
copy made while sitting at another machine (a MacBook) landed on the laptop, silently. Each
request now asks who is attached to THIS session: when the ssh connection that attached it
carried `LC_CLIPBOARD_SINK=osc52`, text copies are written to the session's terminal as an
OSC 52 escape instead, which reaches whatever terminal is at the far end of any number of hops.
A `shpool` session outlives its attaches and keeps the environment it was created with, so the
marker is read from the live `shpool attach` process, per request, never from the session.
Reads are refused in that mode: answering them from the laptop would paste the wrong machine.
"""

import base64
import datetime
import os
import pathlib
import socket
import sys
from collections.abc import Mapping
from enum import StrEnum

DEFAULT_PORT = 18377
BRIDGE_HOST = "127.0.0.1"
TOKEN_PATH = pathlib.Path.home() / ".config" / "clipboard-bridge" / "token"
LOG_PATH = pathlib.Path.home() / ".cache" / "clipboard-bridge.log"
MAX_LOG_BYTES = 1 << 20
# The log records what each tool asked for; it must never be readable by anyone else.
LOG_MODE = 0o600
MODE_MASK = 0o777
CONNECT_DEADLINE_S = 2.0
TRANSFER_DEADLINE_S = 15.0
TEXT_MIME = "text/plain"
TARGETS_TOKEN = "TARGETS"
EXIT_UNAVAILABLE = 1

# X11 names a text selection with an atom, not a MIME type. Forwarding the atom verbatim
# fails twice over: the bridge's MIME check rejects it for having no "/", and `wl-paste
# --type UTF8_STRING` would find nothing on a Wayland clipboard even if it arrived.
X11_TEXT_ATOMS = {"UTF8_STRING", "STRING", "TEXT", "COMPOUND_TEXT", "text/plain;charset=utf-8"}

# Sent by the ssh client of the machine you sit at (`SetEnv LC_CLIPBOARD_SINK=osc52`); the DGX's
# sshd accepts it because its AcceptEnv is `LANG LC_*`.
SINK_ENV = "LC_CLIPBOARD_SINK"
SESSION_ENV = "SHPOOL_SESSION_NAME"
PROC_ROOT = pathlib.Path("/proc")
TTY_PREFIX = "/dev/pts/"
STDIO_FDS = ("0", "1", "2")
TEXT_MIME_PREFIX = "text/"
# Terminals cap an OSC 52 payload (xterm 100 kB of base64); well past that a copy is not text.
OSC52_MAX_BYTES = 1 << 20
OSC52_CLIPBOARD = "c"
OSC52_BEL = b"\x07"


class Mode(StrEnum):
    """The closed set of things a caller can be asking us to do."""

    LIST = "LIST"
    READ = "READ"
    WRITE = "WRITE"


class Selection(StrEnum):
    """The closed set of X11/Wayland selections the bridge can serve.

    PRIMARY was originally refused here on the reasoning that a mouse selection is meaningless
    on a machine with no mouse. That was wrong, and it cost middle-click paste: the selection
    lives on the LAPTOP, where a mouse made it, and this host only ever needs to READ it.
    Claude Code owns the mouse while it runs, so it services a middle click itself by shelling
    out to `wl-paste --primary` / `xclip -selection primary` -- which this refused, every time.
    """

    CLIPBOARD = "CLIPBOARD"
    PRIMARY = "PRIMARY"


class Sink(StrEnum):
    """Where this session's clipboard lives."""

    BRIDGE = "bridge"
    OSC52 = "osc52"


def log(message: str) -> None:
    """Append one line to the shim's log, never failing the caller if it cannot.

    Callers redirect stderr to /dev/null, so without this there is no way to see what a tool
    actually asked for. Only the flag skeleton is recorded: `wl-copy` accepts the text to copy
    as positional arguments, so logging argv verbatim would write clipboard contents to disk.
    The file is created 0600 for the same reason.

    Args:
        message: The line to record.
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
            LOG_PATH.unlink()
        # os.open with the mode up front, rather than open("a") then chmod: the chmod-after
        # form left a window at 0644 on creation, and it never repaired a log an older
        # version had already created world-readable.
        handle = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, LOG_MODE)
        try:
            if (os.fstat(handle).st_mode & MODE_MASK) != LOG_MODE:
                os.fchmod(handle, LOG_MODE)
            os.write(handle, f"{datetime.datetime.now().isoformat(timespec='seconds')} {message}\n".encode())
        finally:
            os.close(handle)
    except OSError:
        pass


def bail(message: str) -> None:
    """Exit non-zero after a one-line note, so a caller's fallback chain proceeds.

    Args:
        message: Short explanation written to stderr.
    """
    log(f"    FAIL {message}")
    print(f"clip-shim: {message}", file=sys.stderr)
    raise SystemExit(EXIT_UNAVAILABLE)


def normalise_target(target: str) -> str:
    """Translate an X11 selection atom into the MIME type the bridge speaks.

    Args:
        target: The `-t` value an xclip caller supplied.

    Returns:
        The equivalent MIME type, or the original if it already looks like one.
    """
    return TEXT_MIME if target.upper() in {atom.upper() for atom in X11_TEXT_ATOMS} else target


def read_proc(path: pathlib.Path) -> bytes:
    """Read a /proc file, treating a vanished process or a refusal as empty.

    Args:
        path: The /proc file.

    Returns:
        Its bytes, or b"" when it cannot be read.
    """
    try:
        return path.read_bytes()
    except OSError:
        return b""


def attached_client(session: str, proc_root: pathlib.Path = PROC_ROOT) -> pathlib.Path | None:
    """Return the /proc entry of the `shpool attach` process serving a session.

    `shpool attach -f NAME` leaves one live attach per session. If a stale one lingers, the
    highest pid is taken as the newest.

    Args:
        session: The shpool session name.
        proc_root: Where /proc is mounted (a seam for tests).

    Returns:
        The attach process's /proc directory, or None when the session has no live attach.
    """
    newest: pathlib.Path | None = None
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        argv = [part.decode("utf-8", "replace") for part in read_proc(entry / "cmdline").split(b"\0") if part]
        if argv and pathlib.Path(argv[0]).name == "shpool" and "attach" in argv[1:-1] and argv[-1] == session:
            if newest is None or int(entry.name) > int(newest.name):
                newest = entry
    return newest


def proc_environ(entry: pathlib.Path) -> dict[str, str]:
    """Return a process's environment.

    Args:
        entry: Its /proc directory.

    Returns:
        The variables, empty when unreadable.
    """
    pairs = (item.decode("utf-8", "replace").partition("=") for item in read_proc(entry / "environ").split(b"\0"))
    return {key: value for key, sep, value in pairs if sep}


def current_sink(environ: Mapping[str, str] = os.environ, proc_root: pathlib.Path = PROC_ROOT) -> Sink:
    """Decide, for this request, whose clipboard the session is using.

    Args:
        environ: This process's environment (a seam for tests).
        proc_root: Where /proc is mounted (a seam for tests).

    Returns:
        OSC52 when the ssh connection that attached the session asked for it, else BRIDGE.
    """
    session = environ.get(SESSION_ENV)
    client = attached_client(session, proc_root) if session else None
    source = environ if client is None else proc_environ(client)
    return Sink.OSC52 if source.get(SINK_ENV) == Sink.OSC52 else Sink.BRIDGE


def parent_pid(pid: int, proc_root: pathlib.Path = PROC_ROOT) -> int:
    """Return a process's parent pid, or 0 when it cannot be read.

    Args:
        pid: The process.
        proc_root: Where /proc is mounted (a seam for tests).

    Returns:
        The parent pid.
    """
    # The command name is parenthesised and may itself contain spaces or ")", so split after the LAST ")".
    fields = read_proc(proc_root / str(pid) / "stat").decode("utf-8", "replace").rpartition(")")[2].split()
    return int(fields[1]) if len(fields) > 1 else 0


def pty_of(pid: int, proc_root: pathlib.Path = PROC_ROOT) -> pathlib.Path | None:
    """Return the /dev/pts device a process holds as stdin, stdout or stderr.

    Args:
        pid: The process.
        proc_root: Where /proc is mounted (a seam for tests).

    Returns:
        The pty's device path, or None when it holds none.
    """
    for fd in STDIO_FDS:
        try:
            target = os.readlink(proc_root / str(pid) / "fd" / fd)
        except OSError:
            continue
        if target.startswith(TTY_PREFIX):
            return pathlib.Path(target)
    return None


def session_tty(environ: Mapping[str, str] = os.environ, proc_root: pathlib.Path = PROC_ROOT, pid: int | None = None) -> pathlib.Path | None:
    """Find the terminal an OSC 52 copy should be written to.

    In a shpool session that is the pty of the live `shpool attach`: it is the ssh connection
    itself, so the escape goes straight to the terminal at the far end, whatever the calling
    process's ancestry (Claude Code's children are often orphaned) and without passing through
    the session's own screen output. Outside shpool it is the nearest ancestor holding a pty:
    the shim's own stdio is piped by its caller and the tool shells between have no terminal,
    so that is the login shell.

    Args:
        environ: This process's environment (a seam for tests).
        proc_root: Where /proc is mounted (a seam for tests).
        pid: Where to start the ancestor walk (default: this process).

    Returns:
        The pty's device path, or None when none is found.
    """
    session = environ.get(SESSION_ENV)
    client = attached_client(session, proc_root) if session else None
    found = pty_of(int(client.name), proc_root) if client else None
    pid = os.getpid() if pid is None else pid
    seen: set[int] = set()
    while found is None and pid > 1 and pid not in seen:
        seen.add(pid)
        found = pty_of(pid, proc_root)
        pid = parent_pid(pid, proc_root)
    return found


def copy_via_osc52(mode: Mode, mime: str | None, payload: bytes | None, selection: Selection, tty_path: pathlib.Path | None = None) -> bytes:
    """Serve a request by writing an OSC 52 escape to the session's terminal.

    Only text copies exist on this route. Everything else fails fast rather than touching the
    laptop's clipboard, because that is the wrong machine for this session.

    Args:
        mode: Which operation was asked for.
        mime: The MIME type of a WRITE.
        payload: The bytes to copy, for WRITE.
        selection: Which selection was asked for.
        tty_path: The pty to write to (default: found by walking up from this process).

    Returns:
        b"", the body of a successful WRITE.
    """
    if mode is not Mode.WRITE:
        bail("this session's clipboard is on your terminal's machine; reading it is not possible over OSC 52")
    if not (mime or "").startswith(TEXT_MIME_PREFIX):
        bail(f"only text can be copied over OSC 52, not {mime}")
    if not payload:
        bail("clearing the clipboard is not possible over OSC 52")
    if len(payload) > OSC52_MAX_BYTES:
        bail(f"{len(payload)} bytes is too much for OSC 52")
    if selection is Selection.PRIMARY:
        # Claude Code sets both selections on every copy; a terminal's machine has no PRIMARY.
        log("    ok skipped PRIMARY (the clipboard write carries the text)")
        return b""
    tty_path = tty_path or session_tty()
    if tty_path is None:
        bail("no terminal found above this process to send the copy to")
    sequence = b"\x1b]52;" + OSC52_CLIPBOARD.encode() + b";" + base64.b64encode(payload) + OSC52_BEL
    try:
        handle = os.open(tty_path, os.O_WRONLY | os.O_NOCTTY)
        try:
            if os.fstat(handle).st_uid != os.getuid():
                bail(f"{tty_path} is not yours")
            pending = memoryview(sequence)
            while pending:
                pending = pending[os.write(handle, pending) :]
        finally:
            os.close(handle)
    except OSError as exc:
        bail(f"cannot write to {tty_path}: {exc.strerror}")
    log(f"    ok {len(payload)} bytes via osc52")
    return b""


def ask_bridge(
    mode: Mode,
    mime: str | None = None,
    payload: bytes | None = None,
    selection: Selection = Selection.CLIPBOARD,
) -> bytes:
    """Send one request to the laptop's clipboard bridge and return its body.

    Args:
        mode: Which operation to perform.
        mime: The MIME type for READ and WRITE.
        payload: The bytes to place on the clipboard, for WRITE.
        selection: Which selection to act on.

    Returns:
        The raw response body, empty for a successful WRITE.
    """
    if current_sink() is Sink.OSC52:
        return copy_via_osc52(mode, mime, payload, selection)
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        bail(f"no token at {TOKEN_PATH} -- run install_clipboard_bridge.sh on the machine you sit at")

    port = int(os.environ.get("CLIPBOARD_BRIDGE_PORT", DEFAULT_PORT))
    header = f"{mode.value} {token} {selection.value}"
    if mime is not None:
        header += f" {mime}"
    if payload is not None:
        header += f" {len(payload)}"

    try:
        conn = socket.create_connection((BRIDGE_HOST, port), timeout=CONNECT_DEADLINE_S)
    except OSError:
        bail(f"no tunnel to {BRIDGE_HOST}:{port} -- on the machine you sit at: Linux 'systemctl --user status ssh-tunnels@<this box>', Mac 'launchctl print gui/$(id -u)/io.github.devbox-kit.tunnels.<this box>' (devbox-kit tunnels/)")

    body = b""
    with conn:
        conn.settimeout(TRANSFER_DEADLINE_S)
        # sshd accepts the forwarded connection here before it knows whether the far end is
        # listening, so a stopped bridge or a sleeping laptop surfaces as a reset partway
        # through the exchange, not as a failed connect. Every socket fault below is the same
        # user-facing condition: no clipboard.
        try:
            stream = conn.makefile("rwb")
            stream.write(header.encode("utf-8") + b"\n")
            if payload is not None:
                stream.write(payload)
            stream.flush()

            status_line = stream.readline().decode("utf-8", "replace").strip()
            if not status_line:
                bail("clipboard bridge closed the connection without replying")
            verdict, _, detail = status_line.partition(" ")
            if verdict != "OK":
                bail(detail or "clipboard bridge refused the request")
            try:
                expected = int(detail)
            except ValueError:
                # Not merely odd: leaving `body` empty and returning 0 here would be the
                # same silent success a short read used to produce.
                expected = -1
                bail(f"clipboard bridge replied with a malformed length {detail!r}")
            body = stream.read(expected)
            # A short read must fail loudly. Callers redirect stderr and judge only by
            # exit status, so accepting a truncated body writes a corrupt image to disk
            # and reports success -- the silent-corruption case this shim exists to avoid.
            if len(body) != expected:
                bail(f"truncated reply: expected {expected} bytes, got {len(body)}")
        except OSError:
            bail("clipboard bridge stopped answering mid-transfer -- is the laptop awake?")
    log(f"    ok {len(body)} bytes")
    return body


def emit(payload: bytes) -> None:
    """Write raw bytes to stdout without any text mangling.

    Args:
        payload: The bytes to emit.
    """
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def value_after(argv: list[str], index: int, flag: str) -> str:
    """Return the argument following a flag, or exit if it is missing.

    Args:
        argv: The full argument list.
        index: Position of the flag itself.
        flag: The flag name, for the error message.

    Returns:
        The following argument.
    """
    if index + 1 >= len(argv):
        bail(f"{flag} needs a value")
    return argv[index + 1]


def send_write(mime: str, payload: bytes, *, explicit_clear: bool, selection: Selection = Selection.CLIPBOARD) -> None:
    """Place bytes on the clipboard, refusing an accidental wipe.

    A zero-byte write is only ever legitimate when the caller actually asked to clear. Any
    other empty payload means this shim failed to understand the arguments, and forwarding it
    would erase the clipboard while returning success.

    Args:
        mime: MIME type of the payload.
        payload: The bytes to place.
        explicit_clear: True only when the caller passed a clear flag.
        selection: Which selection to write to.
    """
    if not payload and not explicit_clear:
        bail("refusing to write an empty clipboard: arguments not understood, and clearing was not requested")
    ask_bridge(Mode.WRITE, mime, payload, selection)


def read_payload(paths: list[str]) -> bytes:
    """Collect the bytes a copy command wants placed on the clipboard.

    Args:
        paths: Positional file arguments, if the caller gave any.

    Returns:
        The file contents, or standard input when no file was named.
    """
    if paths:
        try:
            return b"".join(pathlib.Path(p).read_bytes() for p in paths)
        except OSError as exc:
            bail(f"cannot read {exc.filename}")
    return sys.stdin.buffer.read()


def run_wl_paste(argv: list[str]) -> None:
    """Serve a `wl-paste` invocation.

    Args:
        argv: Arguments after the program name.
    """
    mime = TEXT_MIME
    selection = Selection.CLIPBOARD
    listing = False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-l", "--list-types"):
            listing = True
        elif arg in ("-t", "--type"):
            mime = value_after(argv, index, arg)
            index += 1
        elif arg.startswith("--type="):
            mime = arg.partition("=")[2]
        elif arg in ("-p", "--primary"):
            selection = Selection.PRIMARY
        elif arg in ("-s", "--seat"):
            index += 1
        index += 1
    # The whole argv is parsed before acting: `wl-paste --primary --list-types` put the flags
    # in that order, and returning on sight of -l listed the CLIPBOARD instead.
    if listing:
        emit(ask_bridge(Mode.LIST, selection=selection))
        return
    emit(ask_bridge(Mode.READ, mime, selection=selection))


def run_wl_copy(argv: list[str]) -> None:
    """Serve a `wl-copy` invocation.

    Args:
        argv: Arguments after the program name.
    """
    mime = TEXT_MIME
    selection = Selection.CLIPBOARD
    clear = False
    words: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-t", "--type"):
            mime = value_after(argv, index, arg)
            index += 1
        elif arg.startswith("--type="):
            mime = arg.partition("=")[2]
        elif arg in ("-c", "--clear"):
            clear = True
        elif arg in ("-p", "--primary"):
            selection = Selection.PRIMARY
        elif arg in ("-f", "--foreground", "-o", "--paste-once", "-n", "--trim-newline"):
            pass
        elif arg in ("-s", "--seat"):
            index += 1
        elif not arg.startswith("-"):
            # `wl-copy [options] text to copy` -- the text is positional.
            words.append(arg)
        index += 1

    if clear:
        send_write(mime, b"", explicit_clear=True, selection=selection)
        return
    payload = " ".join(words).encode("utf-8") if words else sys.stdin.buffer.read()
    send_write(mime, payload, explicit_clear=False, selection=selection)


def run_xclip(argv: list[str]) -> None:
    """Serve an `xclip` invocation.

    Args:
        argv: Arguments after the program name.
    """
    target = TEXT_MIME
    selection = "clipboard"
    reading = False
    files: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-t", "-target", "--target"):
            target = value_after(argv, index, arg)
            index += 1
        elif arg in ("-selection", "-sel", "--selection"):
            selection = value_after(argv, index, arg).lower()
            index += 1
        elif arg in ("-o", "-out", "--out"):
            reading = True
        elif arg in ("-i", "-in", "--in"):
            reading = False
        elif arg in ("-d", "-display", "--display", "-l", "-loops", "--loops", "-wait", "--wait"):
            index += 1
        elif arg in ("-f", "-filter", "--filter", "-r", "-rmlastnl", "--rmlastnl", "-verbose", "-quiet", "-silent", "-noutf8"):
            pass
        elif not arg.startswith("-"):
            files.append(arg)
        index += 1

    # CLIPBOARD and PRIMARY are both bridged; SECONDARY is not, and is refused rather than
    # quietly served as one of the others. Real xclip accepts any unambiguous abbreviation of
    # the selection name, so match by prefix. Real xclip also defaults to PRIMARY when no
    # -selection is given; this still defaults to CLIPBOARD, because every caller we serve
    # spells the flag out and a bare `xclip -o` here is far more likely to mean "the
    # clipboard" than "whatever was last selected with a mouse".
    if selection and "clipboard".startswith(selection):
        chosen = Selection.CLIPBOARD
    elif selection and "primary".startswith(selection):
        chosen = Selection.PRIMARY
    else:
        bail(f"the {selection.upper()} selection is not bridged")

    if reading and target.upper() == TARGETS_TOKEN:
        emit(ask_bridge(Mode.LIST, selection=chosen))
        return
    if reading:
        emit(ask_bridge(Mode.READ, normalise_target(target), selection=chosen))
        return
    send_write(normalise_target(target), read_payload(files), explicit_clear=False, selection=chosen)


def main() -> None:
    """Dispatch on the name this shim was invoked under."""
    called_as = pathlib.Path(sys.argv[0]).name
    argv = sys.argv[1:]
    # Positionals are dropped because `wl-copy [options] text to copy` puts clipboard
    # content there; flag values are kept, because -t/-selection/-d values never are, and
    # without them the log cannot say which MIME or selection was asked for.
    shown, keep_next = [], False
    for item in argv:
        if item.startswith("-") or keep_next:
            shown.append(item)
            keep_next = item in ("-t", "--type", "-selection", "-sel", "--selection", "-target", "--target", "-d", "-display")
    log(f"{called_as} {' '.join(shown)}")
    if called_as == "wl-paste":
        run_wl_paste(argv)
    elif called_as == "wl-copy":
        run_wl_copy(argv)
    elif called_as == "xclip":
        run_xclip(argv)
    else:
        bail(f"invoked as {called_as!r}; expected wl-paste, wl-copy or xclip")


if __name__ == "__main__":
    main()
