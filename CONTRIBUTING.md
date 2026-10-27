# Contributing

Issues and pull requests are welcome. Please keep changes scoped and explain how
they preserve the existing permission, mutation, persistence, and recovery boundaries.

## Development checks

```powershell
uv sync --frozen --group dev
uv run python -m pytest -q
npm --prefix web ci
npm --prefix web run lint
npm --prefix web run test
npm --prefix web run build
```

Browser tests and environment requirements are documented in [README.md](README.md).
Paid-model tests and L2 security tests are opt-in; do not add real credentials to fixtures
or turn those suites on in ordinary pull-request CI.

## Regression expectations

- Add tests for changed behavior, especially denial, stale versions, interrupted writes,
  duplicate approvals, process ownership, and restoration after a service restart.
- Preserve deterministic hard DENY and final filesystem version checks.
- Keep private source, Diff content, credentials, and raw recovery material out of Audit/Trace.
- Keep persisted state versioned, reject unsupported future versions, and document migrations.
- Do not present Fake Provider success as real-model task quality or claim L1 is a security boundary.
- Do not commit `.env`, runtime/session databases, recovery blobs, private provider profiles,
  generated test results, node_modules, virtual environments, or local backups.

By contributing, you agree to make your contribution available under this project's
[MIT License](LICENSE). See [SECURITY.md](SECURITY.md) for vulnerability reporting.
