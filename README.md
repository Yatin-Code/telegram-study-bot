# Telegram Study Bot

Notion is the durable planning and study-data source. Telegram is the control
surface for reminders, block completion, doubts, goals, exams and reports.

## Setup

1. Fill `.env` with the existing Notion, Telegram, LLM and timezone values.
2. Install dependencies with `python -m pip install -r requirements.txt`.
3. Run `python -m config.setup_study_workspace` once. It creates an integration-visible
   `Study Bot System` hub and these databases:
   `Daily Plan`, `Work Items`, `Goals`, `Exams`, `Exam Questions`, `Doubt Attempts`,
   and `Class & Teacher Timetable`.
4. Run `python sync.py --once`.
5. Start on Termux with `./run_bot.sh`.

The setup script is idempotent. It never recreates the original Ledger, Doubts
or Revision databases. The existing schedule database/page can be used instead
after it is shared with the integration and mapped to the Daily Plan source.

## Notion-first daily workflow

Write the next study-day rows in `Study Bot - Daily Plan`. Give every row a
`Plan Date`, unique `Sequence`, `Exit Condition`, estimated minutes and (when
known) expected CY. The watcher waits for three quiet minutes after edits,
syncs the mirror, and sends a bounded analysis. It never rewrites your plan
without confirmation.

At execution time, `/next` activates the next row. Log the block using the
existing execution flow. The resulting Ledger row is linked to the Work Item;
Telegram then offers `Plan complete` or `Carry to backlog`.

## Commands

- `/goal 300 CY every day` or `/goal Physics PYQs for 2 hours daily`
- `/exam JEE Main mock on 2026-08-15, maximum 300, target 220`
- `/today`, `/next`, `/backlog`, `/weekly`, `/weak`
- `/attempt doubt title | minutes | approach | stuck point | outcome`
- `/doubts`, `/resolvedoubt doubt | resolution | teacher`, `/reopendoubt doubt`
- `/timetable`
- `/finish_exam exam`, then `/exam_summary` and `/question_review`
- `/complete_exam_analysis exam`

An exam date is marked `Tentative` unless the user explicitly supplies an
official source. Exam scores permit negative marking, but impossible counts and
marks outside the configured range are rejected.

## Safety and evidence

The language model only proposes validated structured data. It cannot write
directly to Notion, invent IDs, or change planner state. Numeric calculations,
priority gates, doubt-attempt counts and adaptive CY targets are deterministic.
SQL access is read-only. Notion titles, notes and page content are treated as
untrusted data rather than instructions.

Two valid doubt attempts, separated by at least thirty minutes, are required
before teacher escalation. A solution-viewed attempt does not count. The bot
does not interrupt protected or high-priority work; a teacher-window reminder
explains its evidence and can be declined.

## Verification

Offline tests are run with `pytest`. The live checks create temporary Notion
records for linked goals, exams, work items, plans, timetables, doubt attempts
and question reviews, verify mirror state, then archive every test page. The
Telegram check verifies `getMe`, command registration, chat access and a
temporary send/delete round trip. Automated inbound user-message testing needs
Telethon API credentials; without those, handler tests use the real LLM and
Notion with a fake Telegram transport.

For a Termux backup, run `bash backup_mirror.sh`. It uses Python's SQLite
`iterdump`, so the external `sqlite3` executable is not required.
