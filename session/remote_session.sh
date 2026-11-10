# shellcheck shell=bash
# remote_session: persistent terminal sessions on a remote Linux box, built on shpool.
#
# Sessions live on the server, so they survive the SSH link dropping and can be
# picked up again from any machine. Works in bash and zsh. Setup: session/README.md.
#
# Settings (export them in ~/.bashrc or ~/.zshrc BEFORE sourcing this file):
#   REMOTE_SESSION_HOST    ssh host alias of the server, from ~/.ssh/config (clients)
#   REMOTE_SESSION_SERVER  the server's own hostname, as `hostname` prints it there;
#                          on that machine commands run locally instead of over ssh
# Then give the command a short name:
#   alias box=remote_session

# Remote shells started by ssh are non-interactive and may not have ~/.cargo/bin on
# PATH, which is where `cargo install shpool` puts it.
# shellcheck disable=SC2016  # expands on the server, not here
_rs_remote_path='PATH="$HOME/.cargo/bin:$PATH"'

# Uptime, memory and whichever GPU tool the box has (NVIDIA or AMD).
_rs_status_cmd='uptime; free -h | head -n 2
if command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv
elif command -v amd-smi >/dev/null 2>&1; then
    amd-smi metric --usage
else
    echo "no nvidia-smi or amd-smi on this machine"
fi'

# A session name is placed inside a remote shell command, so it is checked rather
# than quoted: quoting differs between bash and zsh, a fixed safe charset does not.
_rs_valid_name() {
    case "$1" in
        ''|*[!A-Za-z0-9._-]*) return 1 ;;
        *) return 0 ;;
    esac
}

_rs_is_server() {
    [ -n "${REMOTE_SESSION_SERVER:-}" ] && [ "$(hostname)" = "$REMOTE_SESSION_SERVER" ]
}

_rs_shpool() {
    if command -v shpool >/dev/null 2>&1; then
        shpool "$@"
    elif [ -x "$HOME/.cargo/bin/shpool" ]; then
        "$HOME/.cargo/bin/shpool" "$@"
    else
        echo "remote_session: shpool not found on PATH or in ~/.cargo/bin" >&2
        return 1
    fi
}

# Run a shell command on the server: here if this is the server, else over ssh.
_rs_run() {
    if _rs_is_server; then
        sh -c "$_rs_remote_path; $1"
    elif [ -z "${REMOTE_SESSION_HOST:-}" ]; then
        echo "remote_session: set REMOTE_SESSION_HOST to the server's ssh alias (see session/README.md)" >&2
        return 2
    else
        # shellcheck disable=SC2029
        ssh "$REMOTE_SESSION_HOST" "$_rs_remote_path; $1"
    fi
}

remote_session() {
    local cmd="${1:-main}"
    case "$cmd" in
        ls|list)
            _rs_run 'shpool list'
            ;;
        detach)
            # Disconnect a session from whatever terminal holds it, WITHOUT killing it.
            # No name: detach every attached session.
            if [ -n "${2:-}" ]; then
                _rs_valid_name "$2" || { echo "remote_session: bad session name: $2" >&2; return 2; }
                _rs_run "shpool detach '$2'"
            else
                # shellcheck disable=SC2016  # awk program, expanded by awk on the server
                _rs_run 'shpool list | awk "NR>1 && \$2==\"attached\" {print \$1}" | xargs -r shpool detach'
            fi
            ;;
        kill)
            # No default name: a forgotten argument must not kill the main session.
            [ -n "${2:-}" ] || { echo "remote_session: kill needs a session name (see: ls)" >&2; return 2; }
            _rs_valid_name "$2" || { echo "remote_session: bad session name: $2" >&2; return 2; }
            _rs_run "shpool kill '$2'"
            ;;
        status)
            _rs_run "$_rs_status_cmd"
            ;;
        help|-h|--help)
            cat <<'USAGE'
remote_session [name]        attach to session <name> (default: main), taking it over
                             from any other terminal that holds it; creates it if absent
remote_session ls            list sessions and whether each is attached
remote_session detach [name] disconnect a session without killing it (no name: all)
remote_session kill <name>   end the session and the shell inside it
remote_session status        uptime, memory and GPU use of the server

Use your alias (e.g. `box`) in place of remote_session.
Detach from inside a session: Ctrl-Space then Ctrl-q
USAGE
            ;;
        *)
            # -f: a session holds one terminal at a time, so take it from a stale or
            # remote client rather than failing with "already attached".
            _rs_valid_name "$cmd" || { echo "remote_session: bad session name: $cmd" >&2; return 2; }
            if _rs_is_server; then
                if [ -n "${SHPOOL_SESSION_NAME:-}" ]; then
                    echo "remote_session: already inside session '${SHPOOL_SESSION_NAME}'. Detach (Ctrl-Space Ctrl-q) first." >&2
                    return 1
                fi
                _rs_shpool attach -f "$cmd"
            elif [ -z "${REMOTE_SESSION_HOST:-}" ]; then
                echo "remote_session: set REMOTE_SESSION_HOST to the server's ssh alias (see session/README.md)" >&2
                return 2
            else
                ssh -t "$REMOTE_SESSION_HOST" "$_rs_remote_path; shpool attach -f '$cmd'"
            fi
            ;;
    esac
}
