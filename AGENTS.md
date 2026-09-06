# AGENTS.md

Owner of the rule below: `AGENTS.md`, section "Repository layout", in
overnightworks/marketplace — a change lands there first and is restated here.

## Repository layout

The repository root holds only what a tool must find there — the package
manifest and its lockfile, container files, scanner configuration, the licence —
and the entry documents `README.md`, `AGENTS.md`, `CLAUDE.md` and, where the
repository carries one, `HEART.md`. Everything else lives in the directory of
its owner: a ratchet baseline next to the check that reads it, a fixture next to
its test, a helper under `scripts/`. No catch-all directory (`tooling/`,
`misc/`) and no deeper hierarchy than the owner needs. A root allowlist check in
CI keeps the rule, so nobody has to remember it (operator ruling 06.09.2026).
