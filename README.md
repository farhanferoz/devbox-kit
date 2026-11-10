# devbox-kit

Tools for working on a remote Linux workstation (an NVIDIA DGX Spark, an AMD
Strix Halo box, or anything else you reach over SSH) from a laptop.

Each folder is one self-contained tool with its own README. Take only the ones you want.

| Folder | What it does | Needs on the box | Needs on your machine |
|---|---|---|---|
| [`session/`](session/README.md) | Persistent terminal sessions: `box`, `box ls`, `box kill <name>`, `box status` | shpool (Rust, user service) | ssh, bash or zsh |

## Conventions

- Nothing here holds a host name, user name, address or key. Each tool reads its
  settings from environment variables documented in its README.
- Every tool works in bash and zsh, on Linux and macOS clients.
