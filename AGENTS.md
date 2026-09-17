# Repository guidance

## Workspace and purpose

This is the standalone open-source **QMT Python Bridge** repository.
Canonical checkout on the maintainer's machine: `D:\bigqmt-data-bridge`.
Perform all future bridge development here. Do not require or edit the original private
data-collection repository, import its configuration/history, or change production jobs.
Other contributors may use another checkout path; do not hardcode the maintainer's username.

If a local HANDOFF.md exists, read it before continuing development; it is private,
Git-ignored, and not required in a fresh checkout. Then read the relevant approved
specification and execution status. Preserve unrelated working-tree changes.
Use feature branches; do not merge main, force-push, publish releases, or switch production
as an implied side effect of development. Verify the current branch and remote with Git.

## Current scope

- Delivered package: experimental read-only historical file bridge, with opt-in K-line downloads.
- M1a: embedded runtime capability inventory implemented and tested; this is not a transport.
- M1b memory transport passed the actual Python 3.6.8 synthetic and controlled recovery gates.
  Read-only polling market snapshots and the external archive consumer are implemented;
  actual market acceptance remains separate. Trading is not implemented. Never claim full
  xtquant compatibility or trading readiness from module presence or offline test results.
- Scope and remaining acceptance gates are governed by
  `docs/superpowers/specs/2026-09-11-stock-etf-bridge-design.md`.

## Runtime and safety

- External Python: 3.10+ with its own dependencies. Embedded QMT target observed: Python 3.6.8.
  Do not copy missing binary modules into QMT or bypass broker permissions.
- QMT UI actions belong to the user. Give precise script/configuration/log instructions;
  never automate clicks, stop existing strategies, or restart the client without direction.
- Real-time market payloads must stay in memory; no file IPC or RAM-disk fallback.
  Historical caches and durable order/dedup audit records may persist.
- Do not modify a running embedded worker assuming reload is safe. Imported modules may
  remain cached. Isolate new experiments and arrange explicit user-controlled upgrades.
- First trading scope: ordinary A-share and ETF secondary-market limit orders, simulation first.
  Before any actual simulated order, confirm account, instrument, direction, quantity and price.
  There is no blanket authorization to trade live or switch production collectors.
- Persist intent before native order submission; never retry an unknown order blindly.
  Account/market rules, reliable transport, reconciliation and risk gates precede trading.
- Same-process history downloads and trading readiness must be mutually exclusive in the
  first trading implementation. Separate directories alone do not enforce this.
- Bound waits and report failures promptly. An external timeout does not cancel embedded calls.
  Use UTC/explicit Beijing time, not the machine's local date, for trading-day decisions.

## Verification

Use an existing external Python environment; first check `python --version`. From repo root:

```powershell
python -m pytest tests/ experiments/qualification_v1/test_qualification.py -q
git diff --check
```

Default pytest discovery omits qualification tests. State the exact command/count.
Test changes before implementation when adding behavior; use independent protocol expectations
in tests, not only constants shared with the producer. Review changes independently.
Keep offline, actual terminal, cache-hit and actual-download evidence distinct.
Use explicit `incomplete` status for unexecuted gates. Do not invent missing test evidence.

## Open-source delivery

Maintain README, tests, supporting docs and the repository Skill. Keep any local
HANDOFF.md updated without adding it to Git.
Keep README focused on project purpose, capabilities, usage and product limitations.
Put per-run dates, sample counts, collection progress and debugging history in dedicated
research/validation documents, not README. Link detailed operating guides from README.
The existing Skill covers legacy/cache_only only; auto and memory/trading need separate updates
and validation before claiming coverage. Do not install the Skill automatically.
Keep MIT attribution. Deliver source only: no installer builds, automatic Release or PyPI publish.
Exclude credentials, accounts, raw market data, private configs, logs, evidence and broker binaries.
Use sanitized examples; stage explicit files and inspect the staged diff and outgoing commits.
Commit messages must not contain assistant product names. Verify the intended GitHub remote.
