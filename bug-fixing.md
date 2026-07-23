# telegram-study-bot Bug Audit

> Compiled: 2026-07-22
> Scope: whole bot logic audit focused on reliability, UX, and correctness for AIR 1 prep.

## Implementation status

### Phase 1 completed — 2026-07-23

- [x] Restricted `rows()`/`_rows()` to a validated WHERE-expression grammar and a single-table, read-only SQLite authorizer boundary.
- [x] Rolled back partial sync writes and persisted `sync_meta.last_error` through a separate transaction.
- [x] Serialised async handlers, synchronous write hooks, and the scheduled sync loop with one process-wide lock.
- [x] Dead-lettered queued writes after five failed attempts while retaining them for health inspection.
- [x] Replaced the `received` catch-all with a real LLM assistant response and a useful offline fallback.

Verification: `pytest -q` → **221 passed, 1 deselected**.

### Phase 3 completed independently — 2026-07-23

- [x] Added `bot_identity.py` as the single source for the AIR 1 goal, purpose, safety rules, and Telegram command catalog.
- [x] Injected the shared identity into the intent parser, SQL analyst, domain parsers, and setup AI parser.
- [x] Reused the same identity in the unknown-intent conversational fallback completed in Phase 1.
- [x] Built Telegram's registered command menu and `/help` catalog from the shared command source.

Verification after Phase 3: `pytest -q` → **224 passed, 1 deselected**.

## 🔴 Critical

### 1. Normal messages fall back to useless “received”
- **File**: `bot.py:2655`
- **Problem**: Any message the intent parser cannot classify replies just `received`. The bot looks dumb because it is artificially restricted.
- **Impact**: General help/explanation questions never reach an assistant.

### 2. SQL injection via `where` clause interpolation
- **Files**: `operational_store.py:325`, `study_domain.py:23-28`
- **Problem**: `rows()` builds SQL as `f"... WHERE {where}"`. User input can leak into `where`.
- **Impact**: Malicious input could read or delete arbitrary rows.

### 3. `sync_once` transaction rolls back its own error logging
- **File**: `sync.py:232-258`
- **Problem**: `_record_sync_error` writes inside the same transaction; if `sync_database` raises, the whole transaction rolls back, so `last_error` is never persisted.
- **Impact**: Failures become invisible in `sync_meta`.

### 4. Write paths bypass the sync lock
- **Files**: `logging_flow.py:633-656`, `logging_flow.py:687-713`, `sync.py:465-481`
- **Problem**: `commit_write`, `append_session_notes`, `scheduled_loop` call `sync.sync_once()` instead of `sync.sync_once_locked()`.
- **Impact**: Concurrent syncs can race and corrupt the SQLite mirror.

### 5. `flush_pending` retries broken payloads forever
- **File**: `logging_flow.py:767-789`
- **Problem**: No maximum attempt count; a permanently broken queued write retries every flush forever.

## 🟠 Major

### 6. `catch_all` pending states can conflict
- **File**: `bot.py:2488-2602`
- **Problem**: Strict order of pending checks means if two pending states exist, the later one is silently ignored.

### 7. Pending clarification is never cleared
- **File**: `bot.py:2609-2621`
- **Problem**: After consuming a clarification, the draft is not deleted, so stale clarifications can re-trigger.

### 8. `_handle_question` treats any `⚠️` prefix as a error
- **File**: `bot.py:2752`
- **Problem**: A valid answer starting with that emoji is treated as failure; error detection is fragile.

### 9. `_handle_question` legacy fallback not saved to Q&A history
- **File**: `bot.py:2752-2757`
- **Problem**: After falling back to `query_flow`, the new answer bypasses the `record_qa` guard.

### 10. `plan_facts` sums CY/minutes across all statuses
- **File**: `study_domain.py:995-996`
- **Problem**: Completed/moved/skipped items still count toward daily expected CY.

### 11. Revision coverage checked by substring-matching titles
- **File**: `study_domain.py:1006-1010`
- **Problem**: A plan item titled “Physics PYQ” could falsely count as covering a revision chapter named “Physics”.

### 12. Planner derives suggestions by grepping warning strings
- **File**: `planner.py:66-76`
- **Problem**: Coupled to exact human-readable wording; renaming a warning breaks planner suggestions.

### 13. Goal kind heuristic misclassifies
- **File**: `study_domain.py:918-936`
- **Problem**: Goal titles containing “revision” or “revise” become `Revision` kind even if the intent is opposite.

### 14. `finish_exam` ignores current exam state
- **File**: `study_domain.py:410-445`
- **Problem**: Can re-run on already `Analysing`/`Analysed`/`Completed` exams.

### 15. `record_exam_summary` lower bound too loose
- **File**: `study_domain.py:457-461`
- **Problem**: Accepts `-max_marks` as a valid score.

## 🟡 Moderate

### 16. `query_flow._infer_db()` fragile substring matching
- **File**: `query_flow.py:137-153`
- **Problem**: “I have no doubt” maps to doubts; “revisionist” maps to revision.

### 17. `sql_query_flow._active_filter_error()` bypassable with comments
- **File**: `sql_query_flow.py:69+`
- **Problem**: `archived /*x*/ = 0` satisfies intent but may bypass the regex.

### 18. `intent_parser._extract_json()` not string-aware
- **File**: `intent_parser.py:355+`
- **Problem**: Braces inside JSON strings are counted as structural braces.

### 19. `create_plan_item` relation discarded into text marker
- **File**: `study_domain.py:533-540`
- **Problem**: Local work items lose the Notion relation; `activate_next_plan` regex-parses `planner_note` to recover it.

### 20. `trajectory_warnings` swallows all `plan_facts` errors
- **File**: `advisor.py:224-233`
- **Problem**: Any failure hides overdue-revision/unplanned-backlog warnings.

### 21. `commit_write` swallows all exceptions and queues them
- **File**: `logging_flow.py:633-656`
- **Problem**: Even programming bugs get queued; if queue fails, write is silently lost.

### 22. `draft_store` pending clarification overwrites previous
- **File**: `draft_store.py:227-240`
- **Problem**: New clarification overwrites old unanswered one.

### 23. `scheduled_loop` can die permanently
- **File**: `sync.py:465-481`
- **Problem**: Uncaught exceptions terminate the background sync thread/loop.

### 24. Callback ID parsing brittle with colons
- **Files**: `bot.py` multiple callback handlers
- **Problem**: Draft IDs/tokens containing `:` mis-parse via `split(":")`.

### 25. `today_command` does not use `_reply_markdown`
- **File**: `bot.py:1473-1498`
- **Problem**: `/today` output is plain text even though it contains structured info.

## 🆕 Onboarding UX / Logic Issues

### 26. Onboarding questions are confusing and inline-keyboard flow is awkward
- **Files**: `onboarding.py` (wizard sections), `bot.py` setup handlers
- **Problem**: The inline keyboard is attached at the bottom of a long message. In "loop" sections the user must scroll back up after each typed answer to tap Done/Skip/Hub. There is no persistent bottom bar or reply-keyboard shortcut.
- **Impact**: First-run setup feels broken; users abandon setup.

### 27. `/setup` always clears active wizard state
- **File**: `bot.py:2005-2012`
- **Problem**: Running `/setup` calls `onboarding.clear(chat_id)` unconditionally, so a user in the middle of the wizard loses their place.
- **Impact**: Cannot resume or check progress mid-setup.

### 28. No progress counter in run-all mode
- **Files**: `bot.py:2024-2052`, `onboarding.py:288-312`
- **Problem**: "Run full setup" drops the user into the first incomplete section but never says "step 3 of 10". The user does not know how much remains.

### 29. `chapters` section silently rewrites the subject
- **File**: `onboarding.py:433-457`
- **Problem**: `chapters_prompt()` asks for the first missing subject, but if the user answers with a chapter for a different subject, `apply_answer` still tags it under the prompted subject.
- **Impact**: Wrong subject/chapter mapping in Current Syllabus.

### 30. `timetable` section accepts a 3-part answer when prompt asks for 4 parts
- **File**: `onboarding.py:404-426`
- **Problem**: Prompt says `Subject | day | HH:MM-HH:MM | teacher`, but `len(parts) < 3` is the only rejection, so teacher is optional without explanation.
- **Impact**: User may skip teacher and later wonder why teacher-doubt alerts do not work.

### 31. Onboarding responses are plain text and not Markdown
- **File**: `bot.py:1967-1981`
- **Problem**: `_setup_section_view` builds structured prompts but sends them without `parse_mode=Markdown`, so hints, examples, and section titles do not stand out.

### 32. `commitments` onboarding section is routed through `_handle_remember`
- **File**: `onboarding.py:514-516`, `bot.py:2552-2583`
- **Problem**: The wizard treats the commitments step as a special case via the generic `/remember` flow, so the user sees a different UI pattern (preview + confirm) than other steps, causing confusion.

### 33. No "Previous" or "Back" navigation in wizard
- **Files**: `onboarding.py`, `bot.py:1967-1981`
- **Problem**: Only "Skip ▸", "Done ✅", and "↩ Hub" exist. There is no way to go back one step to fix a mistake without restarting the whole section.

### 34. `apply_answer` returns the literal string `"handled by _handle_remember"`
- **File**: `onboarding.py:512-516`
- **Problem**: This string should never reach the user, but it is a fragile magic constant. If the routing in `bot.py` ever changes, the user could see it.

### 35. Hub shows every section but does not explain what each section does
- **File**: `bot.py:1941-1959`
- **Problem**: A wall of 10 buttons with checkmarks and warnings gives no context for a first-time user.
- **Impact**: Decision paralysis.

## 🤖 AI Empowerment / Hardcoded → AI-Driven Issues

### 36. Bot has no shared identity/purpose context
- **Files**: `intent_parser.py:238`, `sql_query_flow.py:110`, `domain_parser.py`
- **Problem**: LLM prompts only say “personal study-logging Telegram bot.” They never mention AIR 1 goal, commands, or how the bot grows with the user.
- **Impact**: When asked “what can you do?” the bot classifies it as `unknown` and replies `received`.

### 37. No general assistant fallback for `unknown` intents
- **File**: `bot.py:2655`
- **Problem**: `unknown` intents reply `received` instead of invoking the LLM with the bot’s identity and command list.
- **Impact**: Normal questions feel broken.

### 38. Commands/features are hardcoded, not registered
- **File**: `bot.py`
- **Problem**: Inline keyboards and command suggestions are built manually per handler. There is no central action registry with metadata.
- **Impact**: Adding or reordering buttons requires editing multiple handlers.

### 39. AI is not given command/action registry to choose from
- **File**: `bot.py` (all handlers)
- **Problem**: The LLM cannot decide “given this context, show these 3 most useful actions” because there is no registry to select from.
- **Impact**: Every screen shows the same buttons regardless of context.

### 40. Report responses are hardcoded templates
- **Files**: `bot.py:1473-1498` (`/today`), `bot.py:1723+` (`/weekly`), `exam_readiness.py`, `message_templates.py`
- **Problem**: Text reports are assembled from fixed strings instead of letting the LLM synthesize raw data into personalized advice.
- **Impact**: Reports are generic and miss cross-metric connections.

### 41. Planner suggestions grep warning strings
- **File**: `planner.py:66-76`
- **Problem**: The planner derives next actions by searching human-readable warning text, not from structured data.
- **Impact**: Suggestions break if warning wording changes.

### 42. Reminder/nudge messages are static
- **Files**: `reminders.py`, `bot.py` morning/commitment nudge handlers
- **Problem**: Reminders use the same prewritten text regardless of recent performance, goals, or time of day.
- **Impact**: Nudges become noise and are ignored.

### 43. Onboarding is a fixed form, not a conversation
- **Files**: `onboarding.py`, `bot.py:1941-1995`
- **Problem**: 10 fixed sections with fixed prompts. No follow-up questions based on previous answers.
- **Impact**: Onboarding collects operational data but does not build a learner model.

### 44. No learner profile built from onboarding answers
- **Files**: `onboarding.py`, `commitments.py`, `advisor.py`
- **Problem**: Onboarding answers become isolated rows (timezone, capacity, preferences, goals). No derived profile explains “this user learns best in the morning, needs physics push, etc.”
- **Impact**: The bot cannot personalize strategy.

### 45. Doubt resolution is just state changes, not tutoring
- **Files**: `bot.py` `/attempt`, `/resolvedoubt`, `/reopendoubt` handlers, `exam_readiness.py`
- **Problem**: The bot records attempts and marks solved, but never asks follow-up questions to verify real understanding.
- **Impact**: Doubts can be marked resolved prematurely.

### 46. Exam readiness is just coverage gaps, not strategy
- **Files**: `exam_readiness.py`, `study_domain.py`
- **Problem**: Readiness report lists covered/uncovered chapters but does not weigh syllabus weightage, past mistakes, or time remaining.
- **Impact**: User cannot tell which weak chapter to attack first.

### 47. Adaptive scheduling is just fixed cron jobs
- **Files**: `user_jobs.py`, `reminders.py`, `sync.py`
- **Problem**: Reminder times are static settings. The bot does not learn when the user actually responds.
- **Impact**: Reminders fire at the wrong times for that specific user.

---

## ✅ Final Plan

### Phase 1: Stop the bleeding (completed 2026-07-23)
1. [x] Fix SQL injection risk in `operational_store.rows` and `study_domain._rows`.
2. [x] Make `sync_once` record its own errors outside the main transaction.
3. [x] Route all sync entry points through the shared lock.
4. [x] Cap `flush_pending` retry attempts.
5. [x] Replace `received` fallback with a real assistant fallback.

### Phase 2: Fix core UX
6. Fix onboarding UX: persistent bottom keyboard, progress counter, back navigation, Markdown rendering.
7. Clear pending clarification after consumption.
8. Replace `⚠️` prefix error detection with structured response.
9. Make `/today`, `/weekly`, and `/readiness` render Markdown.
10. Fix brittle callback parsing to use JSON or delimiters that do not conflict with IDs.

### Phase 3: Give AI context (completed 2026-07-23)
11. [x] Create a single bot-identity source (goal, commands, purpose, rules).
12. [x] Inject that source into every LLM prompt: intent parser, SQL analyst, setup parser, domain parser.
13. [x] Add a general-assistant fallback for `unknown` intents.

### Phase 4: AI-driven actions
14. Create an `actions.py` registry of every Telegram command/button the bot can do.
15. Give the AI the registry and let it choose which actions to show per context.
16. Render reports through an two-step AI: data → insight → message + buttons.
17. Replace planner string-grep with structured warnings + AI ranking of next actions.

### Phase 5: Make the bot grow with the user
18. Build a learner profile from onboarding + preferences + recent logs.
19. Add a nightly LLM job that extracts one new insight about the user.
20. Use the profile to personalize reminders, nudges, and plan suggestions.

### Phase 6: Smarter coaching loops
21. Make doubt resolution conversational (verify understanding before marking solved).
22. Make exam readiness strategic (rank gaps by weightage, mistakes, and time).
23. Make reminder timing adaptive (learn response times).
24. Make onboarding dynamic and conversation-driven.

### What stays hardcoded
- Database validation
- Sync locks and retries
- Reset confirmation tokens
- Permission checks
- SQL injection guards
- Backup scheduling

These are the safety rails; everything else can move toward AI-driven context and representation.
