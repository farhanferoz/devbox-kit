#!/usr/bin/env python3
"""Serve the clipboard of the machine you are sitting at to a remote host, for one session.

Claude Code and the Antigravity CLI (`agy`) both read the clipboard by shelling out to
`wl-paste` / `xclip` -- verified by inspecting both binaries, which contain the literal
command lines `wl-paste --type image/png` and `xclip -selection clipboard -t TARGETS`.
Over SSH those run on the *remote* host, which has no display server, so every clipboard
read fails and image paste is impossible. Anthropic declined the terminal-native
alternative -- claude-code#42712, asking for OSC 52/5522 support, is closed "not planned"
-- so the shell-out is permanent, and that is what makes it the right thing to intercept.

Nothing runs while idle, and nothing depends on how the session was started. systemd socket
activation holds 127.0.0.1:18377; this process is started on the first clipboard request and
exits after IDLE_EXIT_S of silence. `RemoteForward` in `~/.ssh/config` carries the port to the
remote host, where `clip_shim.py` masquerades as `wl-paste`/`wl-copy`/`xclip` and answers out
of it.

An earlier design wrapped ssh per session (`--with`, still supported for debugging) and it was
broken by construction: a terminal opened before the shell function changed silently had no
bridge. Socket activation removes that whole failure class -- a bare `ssh mybox` works.

The config-level forward is safe ONLY because the socket always accepts. A forward with no
listener behind it makes the owning ssh client print `connect_to ... failed` into its own
terminal on every probe, corrupting a full-screen program. That is why nothing in the
socket-activated path may exit on a startup fault: it answers ERR instead, so the socket keeps
listening even when the token is missing.

No new inbound surface is created: the listener is loopback-only, the forward exists only while
an outbound session is attached, and every request must present a shared token.

Binary-safe by design -- images are the entire point, so payloads are length-prefixed
rather than line-framed.
"""

import hmac
import json
import os
import pathlib
import platform
import re
import shutil
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
from enum import StrEnum
from urllib.parse import unquote, urlsplit

HOST = "127.0.0.1"
DEFAULT_PORT = 18377
MACOS_HELPER_PATH = pathlib.Path.home() / ".local/lib/clipboard-bridge/macos-clipboard-helper"
TOKEN_PATH = pathlib.Path.home() / ".config" / "clipboard-bridge" / "token"
TOOL_DEADLINE_S = 10
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_HEADER_BYTES = 4096
PROBE_DEADLINE_S = 1.0
TEXT_MIME = "text/plain"
PNG_MIME = "image/png"
# systemd hands inherited sockets starting at fd 3 (SD_LISTEN_FDS_START).
SD_LISTEN_FD = 3
IDLE_EXIT_S = 300
IDLE_CHECK_S = 30
SHUTDOWN_JOIN_S = 20

# Parameters matter: `wl-paste --list-types` reports text as `text/plain;charset=utf-8`,
# and a caller may hand that straight back to us. Space is the header delimiter, so it is
# the one character a MIME type may not contain here.
VALID_MIME = re.compile(r"^[A-Za-z0-9!#$&^_.+-]{1,64}/[A-Za-z0-9!#$&^_.+;=-]{1,96}$")

class Verb(StrEnum):
    """The closed set of requests a shim may make."""

    LIST = "LIST"
    READ = "READ"
    WRITE = "WRITE"


class Backend(StrEnum):
    """The closed set of clipboards this can sit in front of."""

    WAYLAND = "wayland"
    X11 = "x11"
    MACOS = "macos"


class Selection(StrEnum):
    """The closed set of selections a shim may ask for.

    Both selections are readable AND writable. A remote Claude Code mirrors a drag-select into
    both, the way a local terminal does, so refusing either one leaves that half stale and the
    user pastes text they did not just select.
    """

    CLIPBOARD = "CLIPBOARD"
    PRIMARY = "PRIMARY"


def detect_backend() -> Backend:
    """Work out which clipboard this machine actually has.

    Returns:
        The backend to drive.
    """
    if platform.system() == "Darwin":
        return Backend.MACOS
    if shutil.which("wl-paste"):
        return Backend.WAYLAND
    if shutil.which("xclip"):
        return Backend.X11
    sys.exit("[clipboard-bridge] no clipboard tool found: install wl-clipboard (Wayland) or xclip (X11)")


def run_tool(args: list[str], payload: bytes | None = None, *, capture: bool = True) -> tuple[int, bytes]:
    """Run a clipboard command and optionally capture its raw stdout.

    `wl-copy` forks and goes on serving the selection after its parent exits, so the
    surviving child inherits and holds open any pipe we hand it. Capturing its output
    would therefore block until something else replaced the clipboard -- write paths must
    give it DEVNULL instead. Read paths do not fork, so they capture normally.

    Args:
        args: Full argument vector, passed without a shell.
        payload: Optional bytes to feed on stdin.
        capture: Whether stdout is wanted; False detaches the output streams.

    Returns:
        A tuple of the exit status and the captured stdout bytes, empty when not captured.
    """
    sink = subprocess.PIPE if capture else subprocess.DEVNULL
    started = time.monotonic()
    debug = os.environ.get("CLIPBOARD_BRIDGE_DEBUG") == "1"
    if debug:
        print(f"[clipboard-bridge] {pathlib.Path(args[0]).name} started", file=sys.stderr, flush=True)
    try:
        done = subprocess.run(args, input=payload, stdout=sink, stderr=sink, check=False, timeout=TOOL_DEADLINE_S)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[clipboard-bridge] {pathlib.Path(args[0]).name} failed after {time.monotonic()-started:.2f}s", file=sys.stderr, flush=True)
        return 1, str(exc).encode("utf-8", "replace")
    if debug:
        print(f"[clipboard-bridge] {pathlib.Path(args[0]).name} exited {done.returncode} after {time.monotonic()-started:.2f}s", file=sys.stderr, flush=True)
    return done.returncode, done.stdout or b""


def run_osascript(script: str) -> tuple[int, bytes]:
    """Run an AppleScript fragment, fed on stdin so its non-ASCII flavour markers survive.

    Args:
        script: The AppleScript source.

    Returns:
        A tuple of the exit status and captured stdout.
    """
    return run_tool(["osascript", "-"], payload=script.encode("utf-8"))


def macos_metadata(kind: str, fallback_script: str) -> tuple[int, bytes]:
    """Use a native pasteboard reader; keep JXA for older installations."""
    if MACOS_HELPER_PATH.is_file():
        return run_tool([str(MACOS_HELPER_PATH), kind])
    return run_tool(["osascript", "-l", "JavaScript", "-"], payload=fallback_script.encode())


def macos_file_urls() -> tuple[int, list[str]]:
    """Read explicit copied file references, never interpret clipboard text as a path."""
    script = '''ObjC.import("AppKit");
var items=$.NSPasteboard.generalPasteboard.pasteboardItems, urls=[];
for(var i=0; i<items.count; i++) {
    var url=ObjC.unwrap(items.objectAtIndex(i).stringForType("public.file-url"));
    if(url) {
        var resolved=$.NSURL.URLWithString(url).filePathURL;
        urls.push(ObjC.unwrap(resolved.absoluteString) || url);
    }
}
JSON.stringify(urls);'''
    status, out = macos_metadata("file-urls", script)
    try:
        urls = json.loads(out)
        if status or not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            return 1, []
        return 0, urls
    except (ValueError, UnicodeError):
        return 1, []


def copied_image_path(urls: list[str]) -> pathlib.Path | None:
    """Accept one local image explicitly copied by the user."""
    if len(urls) != 1:
        return None
    try:
        url = urlsplit(urls[0])
        if url.scheme != "file" or url.netloc not in ("", "localhost") or not url.path.startswith("/"):
            return None
        path = pathlib.Path(unquote(url.path))
        image_extensions = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".gif", ".heic", ".heif", ".webp", ".bmp", ".avif", ".jp2"}
        if path.suffix.lower() not in image_extensions:
            return None
        return path if stat.S_ISREG(path.stat().st_mode) else None
    except PermissionError:
        print("[clipboard-bridge] copied photo is not readable; check macOS Files and Folders permissions for the bridge's Python interpreter", file=sys.stderr, flush=True)
        return None
    except (OSError, ValueError):
        return None


def macos_list_types() -> tuple[int, bytes]:
    """List the clipboard's flavours on macOS, translated to MIME types.

    Returns:
        A tuple of the exit status and newline-separated MIME types.
    """
    # AppleScript's `clipboard info` asks for data sizes and materializes lazy
    # Finder representations (including costly image conversions). Only query
    # NSPasteboard's type names: discovery must not read every clipboard format.
    script = 'ObjC.import("AppKit"); JSON.stringify(ObjC.deepUnwrap($.NSPasteboard.generalPasteboard.types) || [])'
    status, out = macos_metadata("types", script)
    if status != 0:
        return status, b""
    try:
        types = json.loads(out)
        if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
            return 1, b""
    except (ValueError, UnicodeError):
        return 1, b""
    formats = {
        "public.png": PNG_MIME,
        "public.tiff": "image/tiff",
        "public.jpeg": "image/jpeg",
        "com.compuserve.gif": "image/gif",
        "public.utf8-plain-text": TEXT_MIME,
        "public.utf16-external-plain-text": TEXT_MIME,
        "NSStringPboardType": TEXT_MIME,
        "NeXT TIFF v4.0 pasteboard type": "image/tiff",
    }
    found = [formats[t] for t in types if t in formats]
    if "public.file-url" in types:
        status, urls = macos_file_urls()
        if status:
            return 1, b""
        # Finder also offers a TIFF/PNG of its generic file icon. Advertise only
        # formats derived from the referenced file, never that icon.
        found = [mime for mime in found if not mime.startswith("image/")]
        if copied_image_path(urls) is not None:
            found.insert(0, PNG_MIME)
    # pngpaste decodes TIFF/JPEG too, so advertise the PNG that callers can read.
    # Finder often initially offers only TIFF plus its file URL.
    if any(mime.startswith("image/") for mime in found) and shutil.which("pngpaste"):
        found.insert(0, PNG_MIME)
    return 0, ("\n".join(dict.fromkeys(found)) + "\n").encode("utf-8")


def macos_read(mime: str) -> tuple[int, bytes]:
    """Read one flavour off the macOS pasteboard.

    Finder file references take precedence over the generic icon on its pasteboard.
    Ordinary pixel copies use pngpaste, or AppleScript on older installations.

    Args:
        mime: The MIME type wanted.

    Returns:
        A tuple of the exit status and the raw bytes.
    """
    if mime.startswith(TEXT_MIME):
        return run_tool(["pbpaste"])
    if mime != PNG_MIME:
        return 1, b""
    status, urls = macos_file_urls()
    if status:
        return 1, b""
    if urls:
        path = copied_image_path(urls)
        if path is None:
            return 1, b""
        try:
            if path.stat().st_size > MAX_PAYLOAD_BYTES:
                return 1, b""
            if path.suffix.lower() == ".png":
                data = path.read_bytes()
                return (0, data) if data.startswith(b"\x89PNG\r\n\x1a\n") else (1, b"")
            with tempfile.TemporaryDirectory(prefix="devbox-photo-") as scratch:
                output = pathlib.Path(scratch) / "photo.png"
                status, _ = run_tool(["/usr/bin/sips", "-s", "format", "png", str(path), "--out", str(output)])
                if status or not output.is_file() or output.stat().st_size > MAX_PAYLOAD_BYTES:
                    return 1, b""
                data = output.read_bytes()
                return (0, data) if data.startswith(b"\x89PNG\r\n\x1a\n") else (1, b"")
        except PermissionError:
            print("[clipboard-bridge] cannot read copied photo; check macOS Files and Folders permissions for the bridge's Python interpreter", file=sys.stderr, flush=True)
            return 1, b""
        except OSError:
            return 1, b""
    if shutil.which("pngpaste"):
        return run_tool(["pngpaste", "-"])
    with tempfile.NamedTemporaryFile(suffix=".png") as scratch:
        script = f'set f to open for access POSIX file "{scratch.name}" with write permission\nset eof f to 0\nwrite (the clipboard as «class PNGf») to f\nclose access f'
        status, _ = run_osascript(script)
        if status != 0:
            return status, b""
        return 0, pathlib.Path(scratch.name).read_bytes()


def wayland_selection_flags(selection: Selection) -> list[str]:
    """Give the `wl-paste`/`wl-copy` flags for a selection.

    Args:
        selection: The selection wanted.

    Returns:
        The flags to splice into the command line.
    """
    return ["--primary"] if selection is Selection.PRIMARY else []


def x11_selection_flags(selection: Selection) -> list[str]:
    """Give the `xclip` flags for a selection.

    Args:
        selection: The selection wanted.

    Returns:
        The flags to splice into the command line.
    """
    return ["-selection", "primary" if selection is Selection.PRIMARY else "clipboard"]


def clipboard_list(backend: Backend, selection: Selection) -> tuple[int, bytes]:
    """List the MIME types currently on a selection.

    Args:
        backend: The clipboard to query.
        selection: The selection to query.

    Returns:
        A tuple of the exit status and newline-separated types.
    """
    if backend is Backend.WAYLAND:
        return run_tool(["wl-paste", *wayland_selection_flags(selection), "--list-types"])
    if backend is Backend.X11:
        return run_tool(["xclip", *x11_selection_flags(selection), "-t", "TARGETS", "-o"])
    if selection is Selection.PRIMARY:
        return 1, b""
    return macos_list_types()


def clipboard_read(backend: Backend, mime: str, selection: Selection) -> tuple[int, bytes]:
    """Read one MIME type off a selection.

    Args:
        backend: The clipboard to read.
        mime: The MIME type wanted.
        selection: The selection to read.

    Returns:
        A tuple of the exit status and the raw bytes.
    """
    if backend is Backend.WAYLAND:
        return run_tool(["wl-paste", *wayland_selection_flags(selection), "--no-newline", "--type", mime])
    if backend is Backend.X11:
        return run_tool(["xclip", *x11_selection_flags(selection), "-t", mime, "-o"])
    # macOS has one pasteboard and no PRIMARY. Refusing is correct: serving the pasteboard
    # would hand back something the caller did not ask for.
    if selection is Selection.PRIMARY:
        return 1, b""
    return macos_read(mime)


def clipboard_write(backend: Backend, mime: str, payload: bytes, selection: Selection) -> tuple[int, bytes]:
    """Place bytes on the clipboard.

    Only text is supported on macOS: the write direction exists so that copying *out* of
    Claude Code reaches this machine, and that is always text. Putting an image back on a
    macOS pasteboard has no caller here, so it is refused rather than guessed at.

    Args:
        backend: The clipboard to write.
        mime: The MIME type of the payload.
        payload: The bytes to place.
        selection: The selection to write to.

    Returns:
        A tuple of the exit status and an empty body.
    """
    if backend is Backend.WAYLAND:
        return run_tool(["wl-copy", *wayland_selection_flags(selection), "--type", mime], payload=payload, capture=False)
    if backend is Backend.X11:
        return run_tool(["xclip", *x11_selection_flags(selection), "-t", mime], payload=payload, capture=False)
    if selection is Selection.PRIMARY:
        return 1, b""
    if mime.startswith(TEXT_MIME):
        return run_tool(["pbcopy"], payload=payload, capture=False)
    return 1, b""


def load_token() -> str:
    """Read the shared token, failing loudly rather than serving the clipboard unauthenticated.

    Returns:
        The token string, stripped of surrounding whitespace.
    """
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        sys.exit(f"[clipboard-bridge] cannot read token at {TOKEN_PATH}: {exc}")
    if not token:
        sys.exit(f"[clipboard-bridge] token at {TOKEN_PATH} is empty")
    return token


class ClipboardHandler(socketserver.StreamRequestHandler):
    """Answer one clipboard request from a shim on the remote host."""

    def reply_ok(self, payload: bytes) -> None:
        """Send a success response carrying a length-prefixed body.

        Args:
            payload: The raw bytes to return to the caller.
        """
        self.wfile.write(f"OK {len(payload)}\n".encode("ascii"))
        self.wfile.write(payload)

    def reply_err(self, message: str) -> None:
        """Send a failure response.

        Args:
            message: A short single-line explanation.
        """
        self.wfile.write(f"ERR {message}\n".encode("utf-8", "replace"))

    # Without this a peer that connects and says nothing pins a thread for ever, and a
    # declared-but-unsent body blocks the read below indefinitely. Measured.
    timeout = 20

    def handle(self) -> None:
        """Parse and service a single request, never letting an exception escape.

        Every fault here is the same user-facing condition -- no clipboard -- and an escaping
        exception dumps a socketserver traceback into the journal instead. This is the defect
        class that has bitten this project repeatedly (D43 item 3, D56a), so the whole request
        is wrapped rather than each suspect call.
        """
        try:
            self._service()
        except OSError:
            return

    def _service(self) -> None:
        """Parse and service a single request."""
        raw = self.rfile.readline(MAX_HEADER_BYTES)
        if not raw:
            return
        # Compared as BYTES, before any decode. `compare_digest` refuses non-ASCII str, and
        # decoding with "replace" manufactures U+FFFD from arbitrary input -- so decoding
        # first turned a malformed token into a TypeError instead of a rejection.
        fields = raw.strip().split(b" ")
        min_fields = 2
        if len(fields) < min_fields:
            self.reply_err("malformed request")
            return
        if self.server.startup_error:
            # Deliberately generic: this reply reaches a peer that has not authenticated,
            # and the detail names an absolute path. The specifics go to the journal.
            self.reply_err("clipboard bridge unavailable")
            return
        if not hmac.compare_digest(fields[1], self.server.token):
            self.reply_err("bad token")
            return

        # Only an authenticated peer may keep the bridge alive: stamping before this let an
        # unauthenticated connection defeat the idle exit for ever.
        self.server.last_used = time.monotonic()

        verb_text, *rest = [field.decode("utf-8", "replace") for field in fields[:1] + fields[2:]]
        try:
            verb = Verb(verb_text)
        except ValueError:
            self.reply_err(f"unknown verb {verb_text!r}")
            return

        # The selection is its own field rather than a sixth verb: verb x selection is a
        # product, not a longer list of verbs. A shim older than this field sends its MIME
        # here, which fails the Selection lookup below -- an explicit refusal, never a read of
        # the wrong selection.
        if not rest:
            self.reply_err("missing selection")
            return
        selection_text, *rest = rest
        try:
            selection = Selection(selection_text)
        except ValueError:
            self.reply_err(f"unknown selection {selection_text!r}")
            return

        backend = self.server.backend
        if verb is Verb.LIST:
            status, out = clipboard_list(backend, selection)
            self.reply_ok(out) if status == 0 else self.reply_err(f"the {selection.value} selection is empty")
            return

        if not rest or not VALID_MIME.match(rest[0]):
            self.reply_err("missing or malformed mime type")
            return
        mime, *tail = rest

        if verb is Verb.READ:
            status, out = clipboard_read(backend, mime, selection)
            self.reply_ok(out) if status == 0 else self.reply_err(f"nothing on the {selection.value} selection as {mime}")
            return

        # `str.isdigit()` is True for characters int() refuses, such as the superscripts
        # '²' and '⁵', so the conversion itself has to be guarded.
        try:
            length = int(tail[0]) if tail else -1
        except ValueError:
            length = -1
        if length < 0:
            self.reply_err("missing or malformed payload length")
            return
        if length > MAX_PAYLOAD_BYTES:
            self.reply_err(f"payload of {length} bytes exceeds the {MAX_PAYLOAD_BYTES} byte limit")
            return
        payload = self.rfile.read(length)
        if len(payload) != length:
            self.reply_err("truncated payload")
            return
        # PRIMARY was read-only until 2026-08-29, on the reasoning that "the remote reads the
        # selection, it never makes one". MEASURED FALSE: on every drag-select inside a remote
        # Claude Code the app mirrors its selection into BOTH selections, exactly as a local
        # terminal does -- `wl-copy` (allowed) then `wl-copy --primary` (refused). So the
        # clipboard took the new text and PRIMARY kept the old, and middle-click pasted
        # something stale. The security argument was empty too: CLIPBOARD writes are already
        # allowed, so a remote process can ALREADY choose what the next paste yields. Refusing
        # only PRIMARY bought nothing and broke the feature it was meant to protect.
        status, _ = clipboard_write(backend, mime, payload, selection)
        self.reply_ok(b"") if status == 0 else self.reply_err(f"cannot put {mime} on this clipboard")


class ClipboardServer(socketserver.ThreadingTCPServer):
    """A loopback-only clipboard server carrying the shared token and the chosen backend."""

    allow_reuse_address = True
    # Not daemons: a request in flight when the idle timer fires must complete, or the
    # caller receives a truncated body and accepts it as success.
    daemon_threads = False

    def __init__(self, address: tuple[str, int], token: str, backend: Backend, inherited: socket.socket | None = None) -> None:
        """Bind the server, or adopt a socket systemd already bound, and record request state.

        Args:
            address: The (host, port) pair to bind when not inheriting.
            token: The shared secret expected in each request header.
            backend: The clipboard implementation to drive.
            inherited: A already-listening socket passed by systemd, if any.
        """
        super().__init__(address, ClipboardHandler, bind_and_activate=inherited is None)
        if inherited is not None:
            self.socket.close()
            self.socket = inherited
        self.token = token.encode("utf-8")
        self.backend = backend
        self.startup_error = ""
        self.last_used = time.monotonic()


def already_running(port: int) -> bool:
    """Report whether some other bridge already holds the port.

    Args:
        port: The loopback port to probe.

    Returns:
        True when something is listening.
    """
    try:
        with socket.create_connection((HOST, port), timeout=PROBE_DEADLINE_S):
            return True
    except OSError:
        return False


def with_forward(command: list[str], port: int) -> list[str]:
    """Insert the reverse tunnel into an ssh command line, for the `--with` debugging path.

    NOT the normal route. In the shipped design the forward lives in `~/.ssh/config` and the
    laptop end is socket-activated, so every ssh session has a clipboard without a wrapper.
    This exists for debugging and for a machine with no systemd, where socket activation is
    unavailable and a per-session bridge is the only option.

    `-o LogLevel=ERROR` here suppresses the benign "remote port forwarding failed" line a
    second concurrent session emits. It does NOT suppress `connect_to ... failed`, which is
    logged at ERROR -- measured; only QUIET hides that, and hiding it would be wrong, since it
    is the one signal that the far end has stopped listening.

    Args:
        command: The command about to be run.
        port: The loopback port being served.

    Returns:
        The command with the forward inserted, or unchanged if it is not an ssh call.
    """
    if not command or pathlib.Path(command[0]).name != "ssh":
        print(f"[clipboard-bridge] {command[0] if command else 'command'} is not ssh; no tunnel added", file=sys.stderr)
        return command
    forward = ["-o", "LogLevel=ERROR", "-R", f"127.0.0.1:{port}:127.0.0.1:{port}"]
    return [command[0], *forward, *command[1:]]


def serve_during(command: list[str], port: int) -> int:
    """Serve the clipboard for exactly as long as a wrapped command runs.

    Args:
        command: The command to run, typically the ssh invocation.
        port: The loopback port to serve on.

    Returns:
        The wrapped command's exit status.
    """
    server: ClipboardServer | None = None
    if already_running(port):
        print(f"[clipboard-bridge] port {port} already served by another session; not starting a second", file=sys.stderr)
    else:
        server = ClipboardServer((HOST, port), load_token(), detect_backend())
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return subprocess.run(with_forward(command, port), check=False).returncode
    except KeyboardInterrupt:
        return 130
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


def serve_systemd() -> None:
    """Serve on the socket systemd already bound, then exit once idle.

    Socket activation is what makes this robust. systemd owns the listening port, so a
    connection from the tunnel always succeeds no matter how the ssh session was started
    -- there is no shell function to reload and no wrapper to remember. That matters more
    than it sounds: every failure of the wrapper design traced back to a terminal that had
    been open since before the function changed.

    Nothing runs while idle either, because this exits after IDLE_EXIT_S of no requests and
    systemd starts a fresh one on the next connection.
    """

    # Nothing below may exit the process on a startup fault. The whole config-level
    # RemoteForward design rests on the socket ALWAYS accepting: with Accept=no a pending
    # connection keeps the listening fd readable, so a service that dies without accepting is
    # re-triggered instantly, burns systemd's start limit in about two seconds, and the socket
    # unit then stops listening -- at which point ssh writes "connect_to ... failed" into
    # whatever full-screen program is running. An ordinary act like rotating the token must
    # therefore degrade to an ERR reply, never to a dead socket.
    # Catch BaseException, not just SystemExit: a token file containing invalid UTF-8 raises
    # UnicodeDecodeError, and a non-numeric idle override raises ValueError -- both slipped
    # past a SystemExit-only guard and killed the process.
    try:
        if os.environ.get("LISTEN_FDS") is None:
            message = "--systemd requires socket activation; start clipboard-bridge.socket, not the service"
            raise RuntimeError(message)
        token = load_token()
        backend = detect_backend()
        idle_limit = float(os.environ.get("CLIPBOARD_BRIDGE_IDLE_S", IDLE_EXIT_S))
    except (SystemExit, Exception) as fault:  # noqa: B014 - SystemExit is not an Exception
        token, backend, idle_limit, startup_error = "", Backend.WAYLAND, float(IDLE_EXIT_S), str(fault) or repr(fault)
    else:
        startup_error = ""
    inherited = socket.socket(fileno=SD_LISTEN_FD)
    server = ClipboardServer((HOST, DEFAULT_PORT), token, backend, inherited=inherited)
    server.startup_error = startup_error
    if startup_error:
        print(f"[clipboard-bridge] degraded: {startup_error}", file=sys.stderr, flush=True)

    # Not a daemon thread: an in-flight request must finish rather than be cut off at
    # interpreter exit, which would surface to the caller as a truncated body.
    worker = threading.Thread(target=server.serve_forever, daemon=False)
    worker.start()
    while time.monotonic() - server.last_used < idle_limit:
        time.sleep(min(IDLE_CHECK_S, idle_limit))
    server.shutdown()
    worker.join(timeout=SHUTDOWN_JOIN_S)
    # server_close() joins the handler threads, and THAT is the join that can block -- a
    # caller dribbling a body keeps a handler alive well past the idle deadline. Bound it
    # here rather than waiting indefinitely for a hung but legitimate peer.
    closer = threading.Thread(target=server.server_close, daemon=True)
    closer.start()
    closer.join(timeout=SHUTDOWN_JOIN_S)


def main() -> None:
    """Dispatch on the mode requested."""
    argv = sys.argv[1:]
    port = DEFAULT_PORT

    if argv and argv[0] == "--check":
        raise SystemExit(0 if already_running(port) else 1)

    if argv and argv[0] == "--backend":
        print(detect_backend().value)
        raise SystemExit(0)

    if argv and argv[0] == "--systemd":
        serve_systemd()
        raise SystemExit(0)

    if argv and argv[0] == "--with":
        if "--" not in argv:
            sys.exit("[clipboard-bridge] usage: clipboard_bridge.py --with -- <command...>")
        command = argv[argv.index("--") + 1 :]
        if not command:
            sys.exit("[clipboard-bridge] --with needs a command after --")
        raise SystemExit(serve_during(command, port))

    backend = detect_backend()
    with ClipboardServer((HOST, port), load_token(), backend) as server:
        print(f"[clipboard-bridge] serving the {backend.value} clipboard on {HOST}:{port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
