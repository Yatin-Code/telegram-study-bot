# Agent notes

## Verify

```bash
python -m compileall -q .
pytest -q
```

CI (`.github/workflows/offline-tests.yml`) runs the same: `compileall` then `pytest -q` (offline, `-m "not live"`). System python has no pytest — use `.venv-test/bin/python -m pytest -q` locally.

## Live end-to-end test (real router, real providers)

`test_2year_multi_tool.py` builds a temp SQLite mirror of a 2-year dataset (ledger/doubts/revision + op_* tables, deterministic seed with computable trends + an anomaly + archived rows) then runs two batteries with the REAL router (no mocks):

- Part A: 12 data-biased questions via `sql_query_flow.answer_question(db_path=...)`, validated against ground-truth facts (numbers, directions, anti-hallucination).
- Part B: 5 agent tasks (`agent.run` / `continue_run`) covering read-only SQL, write-preview→confirm→DB-mutation flows (log_study_session, create_goal, schedule_reminder), with tool-use counters.

Run: `python test_2year_multi_tool.py` (or `TEST_2YEAR_PART=A|B` for one part; `SKIP_REAL_LLM=1` to skip). IMPORTANT: the test forces `db_path` on `agent_tools.execute_tool/prepare_write/run_prepared_write` via wrappers — their `db_path=DEFAULT_DB_PATH` defaults are bound at definition time, so merely patching the module attribute leaks writes into the real `sqlite_mirror.db`.

## Model routing: curated ladder

- `llm/ladder.py` — curated `Candidate(gateway, model, seed)` pool (currently 36) across 5 gateways: `eaon`, `g4f`, `google`, `groq`, `openrouter`. `ordered(purpose)` = seed re-ranked by probe streaks, latency EMA, certification bonus, traffic cooldown. Adding a model = one `Candidate` line.
- `llm/health.py` — probe tick (bot job, 270s): chat + tool-call pings, batch of 4 (warm tops → recovery → unprobed → stale). Google thinking models eat `max_tokens` on thoughts: those candidates carry enlarged `probe_max_tokens` (flash-lites 128, 3.x/gemini-2.5 1400); default is 16.
- `llm/router.py` — order: ladder (keys-only, max 6 attempts) → certified catalog → legacy env tail. Pool-behavior tests monkeypatch `router._ladder_routes` away; ladder tests restore `_REAL_LADDER_ROUTES`.
- Keys live ONLY in gitignored `.env` / `ai.env`, resolved via `llm/env_loader`. Never read `os.environ` directly for gateway keys.
- Certify a survivor: `python -m llm.certify --candidate <gateway:model> --purpose sql`.

## Rich messages (Bot API 10.1)

- Module: `rich_message.py` — raw httpx calls for `sendRichMessage`, `sendRichMessageDraft`, and `editMessageText(rich_message=...)`.
- Wired into `bot.py` helpers `_reply_markdown` / `_edit_markdown` / `_send_markdown` and `agent_renderer.render`.
- Streaming pattern (`RichStream`): first → `sendRichMessage`; mid → `sendRichMessageDraft`; final → `editMessageText` with `rich_message`. Never bare `editMessageText` mid-stream.
- Agent chat only: `llm.router.stream_complete` → agent `on_stream` → `agent_renderer.AgentChatStreamer` → `RichStream`. Tool-call turns stay non-streaming.
- Capability latch: hard “unknown method” errors skip rich for the process lifetime.
- Disable: `RICH_MESSAGES=0`. Local Bot API: `TELEGRAM_API_BASE=http://...`.
- ALL markdown text (rich + plain fallbacks, bot.py helpers, agent_renderer) passes through `rich_message.sanitize_markdown` before send: intentional `_italic_`/`*bold*`/code/link spans are preserved, stray `_ * ` [ ]` (e.g. `accuracy_ratio`, `question_1`) are backslash-escaped so an unbalanced marker can't degrade the whole message to plain text. Escape is idempotent (already-escaped markers are skipped) because the fallback chain may sanitize twice.

## Data ownership: Notion vs SQLite

- `config/ownership.py` is the single source of truth: `NOTION_OWNED_KEYS = ledger/doubts/revision`; `SQL_OWNED_KEYS = work_items/goals/exams/exam_questions/doubt_attempts/timetable/daily_plan` → physical tables `op_<key>`.
- `sync.DB_TABLES` = ONLY the Notion-owned keys. `sync.init_db` never creates bare mirrors for SQL-owned keys; it also DROPS legacy bare `goals`/`work_items`/... tables when empty or when every row was already migrated into `op_*` (never when rows are unmigrated — `operational_store._import_legacy_notion_rows` still imports them).
- `agent_tools.get_schema(None)` hides bare SQL-owned names; the agent prompt's schema block and `ownership_prompt_block()` state that `op_*` is the ONLY home for these domains. If a bare `goals` query ever appears in a live run, that's a regression.

## Execution discipline (block-level timetable enforcement)

- Module: `execution_discipline.py` — enforces the daily JEE timetable at the block level. Owns 4 local tables (registered in `config.ownership.LOCAL_SQL_TABLES`): `execution_templates` (2 fixed day templates), `execution_blocks` (20 blocks, 10 per template, verbatim from the PDF), `block_confirmations` (per `(local_date, block_key)` state machine: `pending → started | skipped`, plus `completed` auto-derived from ledger evidence), `execution_day_types` (per-date cached day-type resolution).
- Day type: a date is a *coaching day* iff the fresh NTSC cache has ≥1 class for it (`ntsc_coaching.classes_for_date` non-empty AND `coaching_lifecycle.fresh(now=<actual local now>)`); resolved once per local date and cached in `execution_day_types`. `current_block` is midnight-crossing aware and end-inclusive with start-precedence (22:15 → Execution Block C, 01:00 → Sleep, 08:00 → Sleep); between-window gaps return None.
- Escalation (pending blocks only): T0 `start` → T+10 `push` → T+20 `shame`; auto-skip at T+25 or block end (whichever first) applies to `pending` blocks ONLY — a `started` block is never auto-skipped and never gets push/shame; it goes to the post-block check-in instead.
- Post-block check-in: a `started` block with no ledger evidence produces a `discipline_checkin` candidate once it has been over ≥15 min AND the current block is a study block; it regenerates each scan tick until claimed. Ledger evidence in the window auto-completes the block silently (`has_ledger_evidence` uses `created_time` as the authoritative column so crossing-midnight blocks are found).
- Policy: 4 kinds `discipline_start/push/shame/checkin` in `coaching_policy.py`. `start/push/shame` are NOT data-gated (they depend on the local template; day_type already gates coaching-ness); `discipline_checkin` IS gated on `("ledger",)`. All 4 are in `QUIET_BYPASS_KINDS` (safe — the scan only emits them inside study blocks). The scan passes a dedicated `budget_per_day=30` so escalation isn't truncated by the shared 12/day budget.
- Bot wiring: `_execution_discipline_scan` (60s repeating, `_guard_scheduled`, name `execution_discipline_scan`) mirrors the proactive pattern — `run_auto_skip` → `due_escalation_candidates` + `evaluate_completion` → per candidate `decide_notification` → `record_decision` (mandatory) → `reminders.claim` → `discipline_message` → send, release-on-failure. `on_discipline_callback` handles `discipline:start:*` / `discipline:skip:*` inline buttons (registered `CallbackQueryHandler(pattern=r"^discipline:")`).
- LLM writes message text only (`discipline_message`, all 4 tiers) from a bounded redacted fact-only context; on any failure a deterministic fallback is returned. Code decides WHEN; LLM decides WHAT to say. No AIR/rank references anywhere.
- Tests: `test_execution_discipline.py` (unit) + `test_coaching_integration.py` (wiring + full-day drive). Run `pytest test_execution_discipline.py test_coaching_integration.py -k discipline`.

## learner_profile timezone behavior

`learner_profile._rhythm_metrics` converts each ledger row's timestamp to `settings.user_timezone()` (default "UTC") before bucketing into morning/afternoon/evening/night windows. Tests that seed ledger rows with explicit non-UTC offsets (e.g. `+05:30` IST) and assert IST window semantics MUST pin the timezone so they are deterministic in any CI timezone — the repo convention is `monkeypatch.setattr(<module>.settings, "user_timezone", lambda: "Asia/Kolkata")` (see `test_learner_profile.py`, `test_adaptive_reminders.py:31`, `test_coaching_lifecycle.py:37`).
