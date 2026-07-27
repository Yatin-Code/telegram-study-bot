# Live Notion Schema — Daily Execution Ledger

**Database:** 1. Daily Execution Ledger (The 80% Output)  
**ID:** `36dbc6be-f0c2-81db-9da5-f2d1856408ae`

## Properties

| Property | Type | Notes |
|---|---|---|
| `Task (Actionable Verb + Exact Scope)` | title | The study task description |
| `Date` | date | When the session was logged |
| `Subject` | select | Chem / Maths / Physics |
| `Exercise Type` | select | MLE, Ex 1A, Ex 1B, Ex 2A, Ex 2B, Ex 3A, Ex 3B, Ex 4A, Ex 4B, JMYL, JAYL, PYQs, Revision, Theory |
| `Chapter` | relation | → Exercises (Revision) DB |
| `BLOCK` | select | EB-3, EB-1, EB-C, EB-A, EB-B, EB-2, RB, TA, ADV., AB |
| `Actual Time Spent (mins)` | number | Time actually spent in minutes |
| `Questions Attempted` | number | |
| `Questions Correct` | number | |
| `Max Time (min)` | rich_text | |
| `Key Points / Notes` | rich_text | |
| `Doubts` | rich_text | |
| `Alternative` | rich_text | |
| `Id` | unique_id | |
| `Tickbox` | checkbox | |
| `Logged Errors` | relation | → Errors DB |
| `Work Item` | relation | → Work Items & Backlog DB |
| `Accuracy Ratio` | formula | Questions Correct / Questions Attempted |
| `Calculated Circled Qs` | formula | Questions Attempted - Questions Correct |
| `Chapter Text` | formula | Flattened chapter name from relation |
| `Mins per Question` | formula | Actual Time / Questions Attempted |
| `Cognitive Yield` | formula | Complex CY calculation with velocity |
| `Cognitive Yield (Task)` | formula | Task-specific CY |
| `Theory Yield` | formula | Theory-weighted score |
| `Operation ID` | rich_text | Internal tracking |

## Exercises (Revision) DB — Chapter source
**ID:** `36dbc6be-f0c2-81fe-a1d9-ee8def93d63e`

| Property | Type | Notes |
|---|---|---|
| `Chapter / Module` | title | The chapter name |
| `Subject` | select | Chem / Maths / Physics |
| `Exercises` | select | Same options as Exercise Type above |
| `Status` | select | Pending / Completed |
| `Mastery` | status | |
| `Is Short notes Completed` | checkbox | |
| `All Doubts` | rollup | |
| `Chapter Accuracy %` | formula | |
| `Ledger Entries` | relation | Back to Ledger |
| `Total Questions Attempted` | rollup | |
| `Total Questions Correct` | rollup | |
| `Total Time Spent (mins)` | rollup | |
| `Total Circled Qs` | rollup | |
| `Total circled questions (manual)` | number | |
| `Next Execution Date` | date | |
| `Double-Circled (Faculty Intervention Req.)` | number | |
| `Operation ID` | rich_text | |

## Key insights for the agent

- **Chapter is a relation**, not a plain text column. The sync extracts it as `chapter TEXT` (page title) in SQLite.
- **Exercise Type** is the primary classification for what kind of study task.
- **BLOCK** is the difficulty tier (EB-1 is hardest, AB is easiest apparently).
- **Cognitive Yield** and **Theory Yield** are rich Notion formulas — the sync captures them as computed INTEGER values.
- **Questions Attempted/Correct** → stored as `REAL` in SQLite (numbers).
- **Actual Time Spent** → `REAL` in SQLite.
- The ledger can link to **Work Item** (backlog) and **Logged Errors** via relations.
- The Exercises DB is the single source of truth for "what chapters exist".

## Sync extraction mapping (Notion → SQLite `ledger` table)

| Notion property | SQLite column |
|---|---|
| Task (Actionable Verb + Exact Scope) | task TEXT |
| Date | date TEXT |
| Subject | subject TEXT |
| Exercise Type | exercise_type TEXT |
| Chapter (relation → title) | chapter TEXT |
| BLOCK | block TEXT |
| Actual Time Spent (mins) | actual_time_min REAL |
| Questions Attempted | questions_attempted REAL |
| Questions Correct | questions_correct REAL |
| Max Time (min) | max_time_min TEXT |
| Key Points / Notes | key_points_notes TEXT |
| Doubts | doubts TEXT |
| Alternative | alternative TEXT |
| Id (unique_id) | id TEXT |
| Tickbox | tickbox INTEGER |
| Logged Errors (relation) | logged_errors TEXT |
| Work Item (relation) | work_item TEXT |
| Accuracy Ratio (formula) | accuracy_ratio REAL |
| Calculated Circled Qs (formula) | calculated_circled_qs TEXT |
| Chapter Text (formula) | chapter_text TEXT |
| Mins per Question (formula) | mins_per_question REAL |
| Cognitive Yield (formula) | cognitive_yield INTEGER |
| Theory Yield (formula) | theory_yield INTEGER |
| Cognitive Yield (Task) (formula) | cognitive_yield_task TEXT |
| Operation ID | operation_id TEXT |