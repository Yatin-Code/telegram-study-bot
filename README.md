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

## Execution discipline + portal-first onboarding

The bot enforces the daily JEE timetable at the block level. Each local date
resolves to a fixed Coaching or Non-Coaching day template, and every study
block is nudged with a "Time to start" message carrying **Started** / **Skip**
inline buttons. If you never start, it pushes at +10 min, gives a tough coach
talk at +20 min, and auto-skips at +25 min; if you start but don't log real
work, it checks in after the block. Completion is only ever credited from a
real Ledger entry — never inferred.

Setup is portal-first: whatever the Narayana portal mirror already knows
(classes, tests, syllabus, course) is auto-pulled into the setup hub, and you
are only asked for the few things the portal can't give — gradually, one item
per week. Two days before a coaching test, when the bot is confident it knows
your situation, it proposes a focused 2-day plan and asks for confirmation
before writing anything. And once you finish a chapter it tracked from
activation, it proposes tagging it **mastery**, **revision** or **hard** —
you confirm before the tag is saved.

## Commands

- `/goal 300 CY every day` or `/goal Physics PYQs for 2 hours daily`
- `/exam JEE Main mock on 2026-08-15, maximum 300, target 220`
- `/readiness [exam]` audits doubts, attempts, revision and the previous
  seven days of matching key takeaways. `/readiness exam | syllabus` records
  that mock's scope. It never creates Daily Plan rows.
- `/today`, `/next`, `/backlog`, `/weekly`, `/weak`
- `/attempt doubt title | minutes | approach | stuck point | outcome`
- `/doubts`, `/resolvedoubt doubt | resolution | teacher`, `/reopendoubt doubt`
- `/timetable`
- `/finish_exam exam`, then `/exam_summary` and `/question_review`
- `/complete_exam_analysis exam`
- `/reset` opens guarded SQLite, Notion-pages, context, and everything scopes.
  Execution requires an expiring exact sentence with a one-time token.

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

Before an exam, readiness reviews run in the T−7, T−3, T−1 and exam-day
windows. Doubts can be marked still open, solved (only with typed resolution
evidence), or not in that exam. An exam-specific exclusion never closes the
underlying Doubts record globally.

The Notion reset archives pages only. It never deletes or archives database
containers, changes schemas, or changes database IDs. The SQLite reset erases
rows while preserving the database file and its tables, indexes, triggers and
views. Every destructive scope first creates a verified SQLite/settings backup;
an incomplete Notion archive blocks the local-deletion half of `Everything`.

## Verification

Offline tests are run with `pytest`. The live checks create temporary Notion
records for linked goals, exams, work items, plans, timetables, doubt attempts
and question reviews, verify mirror state, then archive every test page. The
Telegram check verifies `getMe`, command registration, chat access and a
temporary send/delete round trip. Automated inbound user-message testing needs
Telethon API credentials; without those, handler tests use the real LLM and
Notion with a fake Telegram transport.

The adversarial gate, live commands, pass criteria and two-year restore/soak
drills are documented in [HARDCORE_TESTING.md](HARDCORE_TESTING.md).

For a Termux backup, run `bash backup_mirror.sh`. It uses Python's SQLite
`iterdump`, so the external `sqlite3` executable is not required.

## Deployment (Azure SentinelVM)

Runs as systemd service `studybot` on SentinelVM (Ubuntu 22.04, Central India),
cloned at `/home/azureuser/studybot` from the private GitHub repo via a
read-only deploy key. Secrets (`.env`), the SQLite store and `settings.json`
live only on the VM — never in git.

Update from the phone (Termux):

    git push
    az vm run-command invoke -g SENTINELRG_INDIA -n SentinelVM \
      --command-id RunShellScript \
      --scripts "sudo -u azureuser git -C /home/azureuser/studybot pull && systemctl restart studybot"

Logs: `ssh azureuser@20.219.16.206 'journalctl -u studybot -n 50 --no-pager'`
