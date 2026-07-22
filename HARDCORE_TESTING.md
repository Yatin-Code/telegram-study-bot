# Hardcore reliability gate

This system has four verification layers. A green layer is evidence about the
software, not a prediction of JEE rank.

## 1. Mandatory offline gate

Run on every code change:

```sh
pytest -q
python -m compileall -q .
```

The gate is deterministic and network-free. It includes randomized formula
invariants, archive isolation, schema migration, crash/outbox behavior,
WAL-consistent backup/restore, leap-day and 731-day persistence, permissions,
planner limits, and time-travel behavior. Pytest return-value warnings are
errors so a test cannot print `BAD`, return `False`, and still appear green.

## 2. Answer-honesty gate

Run the real configured LLM harness three consecutive times:

```sh
python test_answers_groundtruth.py
python test_answers_groundtruth.py
python test_answers_groundtruth.py
```

Each run must score at least 95%, with no critical hallucination. A gateway
failure counts as infrastructure evidence even if fallback later succeeds.

## 3. Live integration gate

These checks use temporary Notion records and clean them up in `finally` paths:

```sh
python test_live_features.py
python test_phase9_e2e.py
python test_handlers_live.py
```

They validate the hybrid SQLite/Notion ownership boundary, Telegram API and
authorization, scheduled jobs, real LLM routing, write/cross-log/sync/query,
and cleanup. Do not run concurrent live write suites against the same account.

## 4. Soak and restore drills

Before treating the bot as dependable for a two-year plan:

- Run it continuously for seven days and triage every `/bug` entry.
- Restore a nightly backup into a separate directory and run `PRAGMA quick_check`.
- Simulate a dead LLM, dead Notion, restart during a pending write, and missed
  nightly job; confirm visible failure and recovery without duplicates.
- Review ground-truth failures monthly and after changing models or prompts.
- Export/retain irreplaceable operational data beyond the seven local daily
  copies; seven days is recovery coverage, not a two-year archival policy.

## Trust boundary

The bot can honestly track supplied data, enforce deterministic gates, and
surface evidence. It cannot verify that self-reported study logs are true,
judge solution quality, validate the CY formula as a scientific predictor, or
guarantee AIR 1. Those require external mock-test calibration, teacher review,
and outcome-based checkpoints.
# Multi-provider LLM routing

Provider traffic is admitted only after a model scores 100% for that purpose:

```bash
python -m llm.certify --purpose intent
python -m llm.certify --purpose domain
python -m llm.certify --purpose sql
```

Certification is resumable by purpose and route. The router stores request,
quota, cooldown, and authoritative rate-header state in `sqlite_mirror.db`.
Eaon is the absolute primary route for every purpose; certified providers are
used only when Eaon fails or its validated output is rejected.
The 90% reserve is advisory: healthy routes below it are preferred, while a
high-quality over-reserve route remains available if every alternative fails.
`/health` reports the last route, fallback state, remaining quota, confidence
(`exact` from provider headers or `estimated`), and reset time.

The no-network abuse gate is:

```bash
pytest -q llm/test_router.py llm/test_quota.py test_failure_drills.py
```

It simulates concurrent reservations, pre-limit switching, 429/reset recovery,
auth disablement, malformed output, 5xx/network fallback, and stale reservations.
