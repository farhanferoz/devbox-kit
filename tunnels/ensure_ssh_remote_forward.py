#!/usr/bin/env python3
"""Add or remove a loopback `RemoteForward` in one host's block of an SSH config, idempotently.

devbox-kit's tunnels carry every forward on ONE always-on connection (tunnels/install.sh), so the
installers REMOVE any matching forward from the host's interactive block: an interactive session
holding a port keeps the tunnel service out. Adding is kept for a machine that runs no service.

A forward in `~/.ssh/config` is safe only when something always listens on the far side: ssh prints
`connect_to ... failed` into its own terminal otherwise, which corrupts a full-screen program like
Claude Code.

Usage:
    ensure_ssh_remote_forward.py <config-path> <host-alias> <port>
    ensure_ssh_remote_forward.py --remove <config-path> <host-alias> <port>
"""

import os
import pathlib
import shutil
import sys

# Kept out of the block-scanning loop below: `LogLevel ERROR` silences the benign "remote port
# forwarding failed" line that a SECOND session gets when the first already bound the port,
# while leaving real faults visible.
QUIET_DIRECTIVE = "    LogLevel ERROR"
EXPECTED_ARGS = 4
# `RemoteForward <bind> <target>` -- a directive with fewer fields cannot be the one we want.
MIN_DIRECTIVE_FIELDS = 2


def directive_for(port: str) -> str:
    """Give the RemoteForward line for a loopback port.

    Args:
        port: The TCP port, forwarded from the remote's loopback to ours.

    Returns:
        The config line, indented for a Host block.
    """
    return f"    RemoteForward 127.0.0.1:{port} 127.0.0.1:{port}"


def has_forward(block: list[str], port: str) -> bool:
    """Say whether a Host block already carries a live RemoteForward for this port.

    Parsed, not substring-matched. A substring test says yes to all three of these, each
    reproduced: a COMMENTED-OUT directive; a different port that merely contains this one as a
    prefix (`47130` contains `4713`); and a directive belonging to a following `Match` stanza.
    Each made the installer print success and insert nothing.

    Args:
        block: The lines of one Host block.
        port: The port that must be forwarded.

    Returns:
        True when an active RemoteForward for exactly this port is present.
    """
    wanted = f"127.0.0.1:{port}"
    for line in block:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) >= MIN_DIRECTIVE_FIELDS and parts[0].lower() == "remoteforward" and parts[1] == wanted:
            return True
    return False


def block_bounds(lines: list[str], host: str) -> tuple[int, int]:
    """Locate one Host block by alias.

    Args:
        lines: The whole config, split into lines.
        host: The Host alias wanted.

    Returns:
        The start index of the `Host` line and the index one past the block's last line.

    Raises:
        SystemExit: If no block for this host exists.
    """
    start = next((i for i, line in enumerate(lines) if line.strip().startswith("Host ") and host in line.split()), None)
    if start is None:
        sys.exit(f"[ERROR] no 'Host {host}' block in the config; add one, then re-run")
    # `Match` ends a Host block just as another `Host` does. Scanning only for `Host ` treated a
    # following Match stanza as part of this block, so a forward sitting in the Match read as
    # already present here.
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip().startswith(("Host ", "Match "))),
        len(lines),
    )
    return start, end


def write_atomically(path: pathlib.Path, text: str) -> None:
    """Replace a file's contents without a window in which it is truncated or partial.

    `write_text` truncates first, so an interruption mid-write loses the user's entire SSH
    config. Low probability, total blast radius -- and this helper is shared by both bridges.

    Args:
        path: The file to replace.
        text: The new contents.
    """
    temp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temp.write_text(text, encoding="utf-8")
    shutil.copymode(path, temp) if path.exists() else temp.chmod(0o600)
    os.replace(temp, path)


def ensure(config_path: pathlib.Path, host: str, port: str) -> str:
    """Ensure the host's block carries the forward, creating the config if absent.

    `Include` directives are not followed: a host defined only in an included file is reported
    as missing rather than silently skipped, which fails loudly and is the correct outcome.

    Args:
        config_path: The SSH config to edit.
        host: The Host alias whose block should carry the forward.
        port: The TCP port to forward.

    Returns:
        A human-readable line describing what changed.

    Raises:
        SystemExit: If the config exists but has no block for this host.
    """
    directive = directive_for(port)
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(f"Host {host}\n{directive}\n{QUIET_DIRECTIVE}\n", encoding="utf-8")
        config_path.chmod(0o600)
        return f"created {config_path} with a '{host}' block"

    lines = config_path.read_text(encoding="utf-8").splitlines()
    start, end = block_bounds(lines, host)
    added = []
    if not has_forward(lines[start:end], port):
        lines.insert(start + 1, directive)
        added.append("RemoteForward")
        end += 1
    if not any(line.strip().lower().startswith("loglevel") for line in lines[start:end]):
        lines.insert(start + 1, QUIET_DIRECTIVE)
        added.append("LogLevel ERROR")
    if added:
        write_atomically(config_path, "\n".join(lines) + "\n")
    return f"{'added ' + ', '.join(added) if added else 'already present'} in the '{host}' block"


def remove(config_path: pathlib.Path, host: str, port: str) -> str:
    """Take every active RemoteForward for this port out of the host's block; nothing else changes.

    Args:
        config_path: The SSH config to edit.
        host: The Host alias whose block should lose the forward.
        port: The TCP port no longer forwarded by interactive sessions.

    Returns:
        A human-readable line describing what changed.

    Raises:
        SystemExit: If the config exists but has no block for this host.
    """
    if not config_path.exists():
        return f"no {config_path}: nothing to remove"
    lines = config_path.read_text(encoding="utf-8").splitlines()
    start, end = block_bounds(lines, host)
    kept = [line for line in lines[start:end] if not has_forward([line], port)]
    if len(kept) == end - start:
        return f"no RemoteForward for {port} in the '{host}' block"
    write_atomically(config_path, "\n".join(lines[:start] + kept + lines[end:]) + "\n")
    return f"removed the RemoteForward for {port} from the '{host}' block (the tunnel service carries it)"


def main() -> None:
    """Run the insertion (or, with --remove, the removal) from command-line arguments."""
    args = sys.argv[1:]
    action = ensure
    if args[:1] == ["--remove"]:
        action, args = remove, args[1:]
    if len(args) != EXPECTED_ARGS - 1:
        sys.exit(f"usage: {pathlib.Path(sys.argv[0]).name} [--remove] <config-path> <host-alias> <port>")
    print("    " + action(pathlib.Path(args[0]), args[1], args[2]))


if __name__ == "__main__":
    main()
