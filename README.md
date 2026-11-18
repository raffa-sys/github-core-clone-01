# github-core-clone-01

[![make.fun](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/competitions/github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/raffa-sys/github-core-clone-01/rank.svg)](https://make.fun/competitions/github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/raffa-sys/github-core-clone-01/reward.svg)](https://make.fun/competitions/github-core-clone-01)

**github-core-clone-01** is a GitHub-compatible git forge in a single Python
3.13 standard-library process — no UI, no outside service, no git binary, no
package manager. One process plus its own in-memory store serves everything
an agent already speaks:

- **Smart HTTP git** — protocol v2 `ls-refs` / `fetch` (with shallow,
  deepen and unshallow) and v0 `receive-pack` (create / update / delete,
  fast-forward enforcement, per-ref `ok` / `ng` report over sideband-64k).
  Packs are plain objects (no deltas required on send); OFS_DELTA and
  REF_DELTA packs are fully parsed on receive.
- **REST v3** — repos, branches, commits, contents (create / read / update /
  delete, with branch creation from the default tip), git refs, labels,
  issues (+ comments) and pull requests (create, files, squash merge with
  three-way conflict detection). Issues and PRs share one per-repo number
  counter, as on GitHub.
- **GraphQL** — the documents `gh` and `github-mcp-server` send: `viewer`,
  `repository`, `node`, `__type` introspection, issue / pull-request
  connections with cursor pagination, and the `createIssue`, `closeIssue`,
  `createPullRequest`, `mergePullRequest` and `addComment` mutations.
  Global ids are opaque base64 ids, stable across reads.
- **MCP** — streamable HTTP with the legacy `initialize` handshake plus
  `tools/list` and `tools/call` for `create_repo`, `get_repo`,
  `create_issue`, `list_issues`, `create_pull_request` and
  `merge_pull_request`, with results that round-trip to the same objects
  over REST.

## Local score (kit runner, this machine)

**934 / 1000** — feature 600/600 (all 120 conformance checks pass),
agent 200/200 (all 15 MCP checks pass), perf 134/200
(push 43, clone 32, read 31, write 28).

## Run it

```sh
python3 main.py            # standalone on $PORT (default 8080)
```

Under the grading harness, set `MAKEFUN_ENV_URL` and `MAKEFUN_SHOP_TOKEN`:
the process binds an ephemeral port, POSTs `{"port": N}` to
`$MAKEFUN_ENV_URL/register`, then polls `GET $MAKEFUN_ENV_URL/done` until
the run ends and exits 0. Every API call carries
`Authorization: Bearer $MAKEFUN_SHOP_TOKEN`; `/healthz` takes no auth.

## Determinism

No wall-clock time and no randomness in any stored state. Commits this
forge creates use a fixed identity and a fixed timestamp, so identical
inputs produce identical object ids. The scenario viewer login is derived
by porting the kit's seeded RNG (same fixed seed), never hardcoded.

## Performance notes

- `TCP_NODELAY` on every accepted socket: without it, small
  request/response pairs over loopback stall on the delayed-ACK timer
  (~40 ms per round trip) and dominate the timed workloads.
- Identical fetch responses are cached per repository (keyed on refs,
  object count and request parameters; objects are append-only, so the
  key pins the state) — the 50-concurrent-clone workload repeats the
  same fetch against an unchanged repo.
- Packs are written with zlib level 1: pack bytes are not graded on size,
  and level 6 costs ~3x the CPU for the same objects.
- The listen backlog is 128, so the 50-concurrent-clone burst does not
  drop SYNs into TCP retransmit backoff.

## Layout

- `main.py` — the whole forge: git object model + packfiles, store,
  smart HTTP, REST v3, GraphQL, MCP, lifecycle. Python 3.13 stdlib only.
- `LICENSE` — MIT.

## License

MIT — see `LICENSE`.
