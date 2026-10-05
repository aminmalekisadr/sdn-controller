# Notes for Claude Code

Read these before doing anything:
- `docs/SPEC.md`: the spec. Section 0 says how to work: stop and report at the end of each phase, ask before working around anything unclear.
- `docs/DECISIONS.md`: every design decision (D1–D73) and the user's answers ("Answers received").
- `docs/PROGRESS.md`: what is done, and the open questions under "Next".

## Status

v1 is done: SPEC phases 0–8, git tag `v1`. Nothing for v2 is built yet. The next step is the user's answers to the v2 questions in `docs/PROGRESS.md`.

## Rules

- Never add Claude attribution (`Co-Authored-By: Claude ...`) to commits or PR descriptions. The user removed it from the history.
- Commit or push only when the user asks.
- `reference/` belongs to the user, for cross-checks only: never edit it, never import it from `src/`, never point `--out` at it. Never edit `golden.json` or a golden number to make a test pass.
- Record each decision in `docs/DECISIONS.md` and keep `docs/PROGRESS.md` current.

## Setup

```
uv venv .venv
uv pip install -e ".[dev]"
bash scripts/check.sh                                   # ruff, mypy, pytest: 266 tests
.venv/Scripts/sdnctl-sim --scenario scenarios --out build/runs   # every scenario (bin/ on Linux and macOS)
```
