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
