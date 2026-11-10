"""Behaviour tests for the clipboard shim's argument parsing.

These exist because the shim had NO tests, and that is precisely how the PRIMARY selection
came to be refused for two days without anyone noticing: every middle-click paste failed, the
shim logged the failure, and nothing ever read it. The bug lived entirely in argument parsing
-- which selection, which MIME, which verb a given argv means -- so that is what these pin.

The assertions are on the REQUEST THE SHIM WOULD SEND, not on whether a function exists. A
test that checks a code path is present is not a test that checks it does the right thing;
this repo has now learned that three times (D43 item 2, D47, D61).

No network: `ask_bridge` is replaced with a recorder, so nothing here touches the real bridge
or the real clipboard.
"""

import importlib.util
import pathlib
import sys

import pytest

SHIM_PATH = pathlib.Path(__file__).resolve().parent.parent / "clip_shim.py"
TEXT = "text/plain"


def load_shim():
    """Import clip_shim.py by path, since it is a script rather than a package member.

    Returns:
        The imported module.
    """
    spec = importlib.util.spec_from_file_location("clip_shim", SHIM_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def shim(monkeypatch):
    """Give a shim module whose bridge calls are recorded instead of sent.

    Args:
        monkeypatch: pytest's patcher.

    Returns:
        A tuple of the module and the list that receives (mode, mime, payload, selection).
    """
    module = load_shim()
    calls = []

    def record(mode, mime=None, payload=None, selection=module.Selection.CLIPBOARD):
        calls.append((mode.value, mime, payload, selection.value))
        return b""

    monkeypatch.setattr(module, "ask_bridge", record)
    monkeypatch.setattr(module, "emit", lambda body: None)
    monkeypatch.setattr(module, "log", lambda message: None)
    return module, calls


# --- wl-paste -------------------------------------------------------------------------


def test_wl_paste_defaults_to_clipboard_text(shim):
    """A bare `wl-paste` reads text from CLIPBOARD."""
    module, calls = shim
    module.run_wl_paste([])
    assert calls == [("READ", TEXT, None, "CLIPBOARD")]


def test_wl_paste_primary_reads_primary(shim):
    """`--primary` must reach the PRIMARY selection -- the two-day bug."""
    module, calls = shim
    module.run_wl_paste(["--primary", "--no-newline"])
    assert calls == [("READ", TEXT, None, "PRIMARY")]


@pytest.mark.parametrize("argv", [["--primary", "--list-types"], ["--list-types", "--primary"]])
def test_wl_paste_list_types_respects_primary_in_either_order(shim, argv):
    """Listing must not act before the whole argv is parsed.

    The first version returned on sight of `-l`, so `wl-paste --primary --list-types` listed
    the CLIPBOARD. Flag order must not change the answer.
    """
    module, calls = shim
    module.run_wl_paste(argv)
    assert calls == [("LIST", None, None, "PRIMARY")]


def test_wl_paste_type_and_primary_combine(shim):
    """An explicit MIME and PRIMARY are independent."""
    module, calls = shim
    module.run_wl_paste(["--type", "image/png", "--primary"])
    assert calls == [("READ", "image/png", None, "PRIMARY")]


# --- wl-copy --------------------------------------------------------------------------


def test_wl_copy_refuses_an_unexplained_empty_write(shim, monkeypatch):
    """An empty write that was not asked for must be refused, never sent.

    Sending it WIPES the laptop clipboard while reporting success -- the defect the module
    docstring records as C3.
    """
    module, calls = shim
    # Replace sys.stdin wholesale: pytest's own stand-in exposes `buffer` as a read-only
    # property, so patching the attribute in place raises rather than substituting.
    monkeypatch.setattr(module.sys, "stdin", type("Stdin", (), {"buffer": type("B", (), {"read": staticmethod(lambda: b"")})()})())
    with pytest.raises(SystemExit):
        module.run_wl_copy([])
    assert calls == []


def test_wl_copy_clear_is_allowed_through(shim):
    """`--clear` is an explicit request to empty the clipboard, so it is sent."""
    module, calls = shim
    module.run_wl_copy(["--clear"])
    assert calls == [("WRITE", TEXT, b"", "CLIPBOARD")]


def test_wl_copy_primary_is_forwarded(shim):
    """A write to PRIMARY must be SENT, because that is how a drag-select gets home.

    This was refused until 2026-08-29 on the reasoning that "the remote reads the selection, it
    never makes one". Measured false: on every drag-select a remote Claude Code mirrors its
    selection into both selections, exactly as a local terminal does. Refusing this half left
    the mouse selection stale, so middle-click pasted text the user had not just selected while
    Ctrl+Shift+V -- which reads the CLIPBOARD half, always allowed -- looked perfectly fine.
    """
    module, calls = shim
    module.run_wl_copy(["--primary", "hello"])
    assert calls == [("WRITE", TEXT, b"hello", "PRIMARY")]


def test_xclip_write_to_primary_is_forwarded(shim, tmp_path):
    """The same write-back on the xclip path, which takes a file rather than stdin."""
    module, calls = shim
    payload = tmp_path / "p.txt"
    payload.write_bytes(b"data")
    module.run_xclip(["-selection", "primary", "-i", str(payload)])
    assert calls == [("WRITE", TEXT, b"data", "PRIMARY")]


# --- xclip ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selection,expected", [("primary", "PRIMARY"), ("prim", "PRIMARY"), ("p", "PRIMARY"), ("clipboard", "CLIPBOARD"), ("clip", "CLIPBOARD"), ("c", "CLIPBOARD")]
)
def test_xclip_selection_abbreviations(shim, selection, expected):
    """Real xclip accepts any unambiguous abbreviation, so the shim must too."""
    module, calls = shim
    module.run_xclip(["-selection", selection, "-o"])
    assert calls == [("READ", TEXT, None, expected)]


@pytest.mark.parametrize("selection", ["secondary", "sec", "nonsense", ""])
def test_xclip_refuses_selections_it_cannot_serve(shim, selection):
    """An unservable selection is refused, never quietly served as a different one."""
    module, calls = shim
    with pytest.raises(SystemExit):
        module.run_xclip(["-selection", selection, "-o"])
    assert calls == []


def test_xclip_defaults_to_clipboard_not_primary(shim):
    """A deliberate divergence from real xclip, which defaults to PRIMARY."""
    module, calls = shim
    module.run_xclip(["-o"])
    assert calls == [("READ", TEXT, None, "CLIPBOARD")]


def test_xclip_targets_lists_the_named_selection(shim):
    """`-t TARGETS -o` is a LIST, and it must honour the selection."""
    module, calls = shim
    module.run_xclip(["-selection", "primary", "-t", "TARGETS", "-o"])
    assert calls == [("LIST", None, None, "PRIMARY")]


def test_xclip_image_from_file_writes_the_file_not_stdin(shim, tmp_path):
    """`xclip -t image/png -i shot.png` must send the FILE.

    Falling through to a stdin read here sent a zero-byte write and wiped the clipboard while
    reporting success -- how Claude Code copies a screenshot.
    """
    module, calls = shim
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nDATA")
    module.run_xclip(["-selection", "clipboard", "-t", "image/png", "-i", str(png)])
    assert calls == [("WRITE", "image/png", b"\x89PNG\r\n\x1a\nDATA", "CLIPBOARD")]


def test_x11_text_atoms_become_a_mime_type(shim):
    """`UTF8_STRING` is an X11 atom, not a MIME type; the bridge only speaks MIME."""
    module, _ = shim
    assert module.normalise_target("UTF8_STRING") == TEXT
    assert module.normalise_target("image/png") == "image/png"


# --- wire format ----------------------------------------------------------------------


def test_request_header_carries_the_selection(monkeypatch, tmp_path):
    """The selection must appear on the wire, positioned after the token.

    A bridge older than this field sees the selection where it expects a MIME type and refuses
    it, which is the required fail-safe: never a read of the wrong selection.
    """
    module = load_shim()
    token = tmp_path / "token"
    token.write_text("SECRET", encoding="utf-8")
    monkeypatch.setattr(module, "TOKEN_PATH", token)
    sent = {}

    class FakeStream:
        def write(self, data):
            sent.setdefault("bytes", b"")
            sent["bytes"] += data

        def flush(self):
            pass

        def readline(self):
            return b"OK 0\n"

        def read(self, length):
            return b""

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def settimeout(self, value):
            pass

        def makefile(self, mode):
            return FakeStream()

    monkeypatch.setattr(module.socket, "create_connection", lambda addr, timeout: FakeConn())
    module.ask_bridge(module.Mode.READ, TEXT, selection=module.Selection.PRIMARY)
    assert sent["bytes"].split(b"\n")[0] == b"READ SECRET PRIMARY text/plain"


# --- whose clipboard: bridge to the laptop, or OSC 52 to the terminal you sit at ---------------


def fake_proc(root, pid, argv=(), env=(), stat_ppid=None, fds=None):
    """Lay out one fake /proc/<pid> entry.

    Args:
        root: The fake /proc directory.
        pid: Process id.
        argv: Command line words.
        env: ``KEY=value`` strings.
        stat_ppid: Parent pid to put in ``stat`` (the command name deliberately contains a space and a ")").
        fds: Mapping of fd number to link target.
    """
    entry = root / str(pid)
    (entry / "fd").mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in argv))
    (entry / "environ").write_bytes(b"".join(e.encode() + b"\0" for e in env))
    if stat_ppid is not None:
        (entry / "stat").write_text(f"{pid} (a b) c) S {stat_ppid} 0 0\n")
    for fd, target in (fds or {}).items():
        (entry / "fd" / fd).symlink_to(target)


def test_sink_follows_the_client_attached_now_not_the_session(tmp_path):
    """The marker is read from the live `shpool attach`, per request.

    A shpool session keeps the environment of whoever created it, so a session started at the
    laptop and later attached from the MacBook must follow the MacBook. A different session's
    attach, or a session nobody is attached to, must never decide.
    """
    module = load_shim()
    fake_proc(tmp_path, 100, ["/home/u/.cargo/bin/shpool", "attach", "-f", "work"], ["LC_CLIPBOARD_SINK=osc52", "TERM=xterm"])
    fake_proc(tmp_path, 101, ["shpool", "attach", "-f", "other"], ["TERM=xterm"])
    fake_proc(tmp_path, 102, ["vim", "work"], ["LC_CLIPBOARD_SINK=osc52"])
    sink = lambda env: module.current_sink(env, tmp_path)  # noqa: E731
    assert sink({"SHPOOL_SESSION_NAME": "work"}) is module.Sink.OSC52
    assert sink({"SHPOOL_SESSION_NAME": "other"}) is module.Sink.BRIDGE
    assert sink({"SHPOOL_SESSION_NAME": "detached", "LC_CLIPBOARD_SINK": "osc52"}) is module.Sink.OSC52  # no attach: fall back to own env
    assert sink({"SHPOOL_SESSION_NAME": "other", "LC_CLIPBOARD_SINK": "osc52"}) is module.Sink.BRIDGE  # the attach, not the session env, decides
    assert sink({}) is module.Sink.BRIDGE
    assert sink({"LC_CLIPBOARD_SINK": "bridge"}) is module.Sink.BRIDGE


def test_osc52_copy_reaches_the_terminal_and_never_the_bridge(tmp_path, monkeypatch):
    """Text goes out as one OSC 52 escape; everything else fails fast without touching the laptop."""
    module = load_shim()
    tty = tmp_path / "pts"
    tty.write_bytes(b"")
    monkeypatch.setattr(module, "log", lambda message: None)
    monkeypatch.setattr(module, "current_sink", lambda: module.Sink.OSC52)
    monkeypatch.setattr(module, "session_tty", lambda: tty)
    monkeypatch.setattr(module.socket, "create_connection", lambda *a, **k: pytest.fail("the laptop bridge was contacted"))
    monkeypatch.setattr(module, "TOKEN_PATH", tmp_path / "no-token")
    module.ask_bridge(module.Mode.WRITE, TEXT, b"hello", module.Selection.CLIPBOARD)
    assert tty.read_bytes() == b"\x1b]52;c;aGVsbG8=\x07"
    module.ask_bridge(module.Mode.WRITE, TEXT, b"hello", module.Selection.PRIMARY)  # PRIMARY is skipped, not duplicated
    assert tty.read_bytes() == b"\x1b]52;c;aGVsbG8=\x07"
    for refused in (
        (module.Mode.READ, TEXT, None),
        (module.Mode.LIST, None, None),
        (module.Mode.WRITE, "image/png", b"\x89PNG"),
        (module.Mode.WRITE, TEXT, b""),
    ):
        with pytest.raises(SystemExit):
            module.ask_bridge(*refused)
    assert tty.read_bytes() == b"\x1b]52;c;aGVsbG8=\x07"


def test_session_tty_prefers_the_attach_client_then_the_nearest_ancestor(tmp_path):
    """In shpool the escape goes to the attach client's pty (ssh itself), even for an orphaned caller.

    Outside shpool the shim and the tool shell hold pipes; the pty belongs to the process above them.
    """
    module = load_shim()
    fake_proc(tmp_path, 300, stat_ppid=250, fds={"0": "pipe:[1]", "1": "pipe:[2]"})
    fake_proc(tmp_path, 250, stat_ppid=200, fds={"1": "pipe:[3]"})
    fake_proc(tmp_path, 200, stat_ppid=150, fds={"2": "/dev/pts/7"})
    fake_proc(tmp_path, 150, stat_ppid=1, fds={"0": "/dev/pts/9"})
    assert module.session_tty({}, tmp_path, 300) == pathlib.Path("/dev/pts/7")
    fake_proc(tmp_path, 400, stat_ppid=1, fds={"0": "pipe:[4]"})  # orphaned: `setsid` children are reparented to init
    assert module.session_tty({}, tmp_path, 400) is None
    fake_proc(tmp_path, 500, ["shpool", "attach", "-f", "work"], fds={"0": "/dev/pts/3", "1": "/dev/pts/3"})
    assert module.session_tty({"SHPOOL_SESSION_NAME": "work"}, tmp_path, 400) == pathlib.Path("/dev/pts/3")
    assert module.session_tty({"SHPOOL_SESSION_NAME": "work"}, tmp_path, 300) == pathlib.Path("/dev/pts/3")  # the attach wins over an ancestor's pty


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
