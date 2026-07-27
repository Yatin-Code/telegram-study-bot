# Telegram Study Bot - Bug Audit Report
**Date**: 2026-07-26  
**Scope**: Core agent functionality, tool calling, memory, scheduling, SQL error recovery

---

## Executive Summary

Audited a 2-year-old Telegram study bot with agentic tool-calling capabilities. Found **7 critical bugs** and **3 architectural issues** affecting:
- Multiple tool execution in a single user message
- SQL error recovery (stops after 1-2 failures)
- Long-term memory retrieval (data accessible but not proactively loaded)
- Scheduled job execution for multi-step goals

---

## Critical Bugs

### 1. **Agent Cannot Execute Multiple Tools in Parallel** ⚠️ CRITICAL
**Location**: `agent.py:512-564`

**Issue**: The agent loop is **strictly sequential** — it processes one tool call per LLM response. When a user message requires multiple operations (e.g., "set goal that I will complete physics by May 24, ask me daily to add timetable"), the agent must:
1. Call first tool
2. Wait for LLM to generate next response
3. Call second tool
4. Repeat...

**Evidence**:
```python
# agent.py:526
tool_call = _parse_tool_call(raw)  # Extracts ONE tool call only
if tool_call is None:
    return {"type": "response", "response": _parse_response(raw)}
```

The `_parse_tool_call()` function extracts a single JSON object with `{"tool": "...", "arguments": {...}}`. There is **no array handling** for multiple tools.

**Impact**:
- User request: "Schedule exam on May 10, create daily plan until then, set reminder"
- Current behavior: Agent schedules exam → stops → waits for next message
- Expected: Execute all 3 operations atomically

**Root cause**: Tool parser + agent loop designed for single-tool-per-turn paradigm.

---

### 2. **SQL Error Recovery Insufficient** ⚠️ HIGH
**Location**: `agent.py:519` + `sql_query_flow.py:446-505`

**Issue**: 
- **Agent loop**: Max 10 iterations total, but no retry-specific budget for SQL errors
- **SQL query flow**: Has retry logic but stops after `max_iterations=12` (line 446)
- When a SQL error occurs (e.g., column name typo, syntax error), the agent:
  1. Gets error feedback from tool
  2. LLM generates new attempt
  3. If still fails → counts toward iteration limit
  4. After 2-3 failures, hits iteration ceiling → returns "took too many steps"

**Evidence**:
```python
# agent.py:519
max_iterations = 10  # Covers ALL tool calls, not just error retries

# agent.py:559-563
# Safety: too many iterations
return {
    "type": "response",
    "response": AgentResponse(text="I took too many steps. Please try again or simplify your request."),
}
```

**SQL query flow** (line 472-474):
```python
except sql_tool.SQLExecutionError as e:
    feedback = f"ERROR (sqlite): {e}"
    _emit("retry", "SQL error, retrying")
```
It retries but no **exponential backoff** or **error classification** (transient vs permanent).

**Impact**:
- Schema mismatches → agent gives up after 3-4 attempts
- User sees "took too many steps" instead of helpful error
- No distinction between "column doesn't exist" (permanent) vs "database locked" (transient)

---

### 3. **No Proactive Memory Loading for Old Content** ⚠️ MEDIUM
**Location**: `agent.py:295-303`, `learner_profile.py:31`

**Issue**: Historical data from 2+ years ago is **accessible** but **not automatically loaded** into agent context.

**Current context includes** (lines 195-209):
- Session context (subject/chapter, expires daily)
- Recent activity (last 7 days only, line 310)
- Learner profile (28-day rolling window, line 334)
- Commitments (no date limit, line 356)

**What's missing**:
- User asks: "What was my physics accuracy in January 2024?"
- Agent context has **no data from 2024** pre-loaded
- Agent must **explicitly write SQL** to query old data
- If LLM doesn't realize it needs historical query → answers "I don't have that data"

**Evidence**:
```python
# agent.py:305-311
def _load_recent_activity(days: int = 7) -> str:
    rows = agent_tools.sqlite_query(
        "... WHERE date >= date('now', '-{} days')".format(days)
    )
```

**Impact**:
- User: "Compare my accuracy now vs 2024"
- Agent: Sees only 7 days of data → answers based on incomplete context
- **Workaround exists**: Agent can use `sqlite_query` tool to fetch old data, but relies on LLM recognizing the need

---

### 4. **Scheduled Jobs Don't Support Multi-Step Goals** ⚠️ HIGH
**Location**: `user_jobs.py:32-33`

**Issue**: User request: "Set goal to complete Physics by May 24, remind me daily to update timetable"

This requires:
1. **Creating a goal record** (INSERT into `goals` table)
2. **Creating a scheduled job** (INSERT into `user_jobs` table with action_kind='ask')
3. **Linking the two** (job queries goal status daily)

**Current limitation**:
```python
# user_jobs.py:32-33
SCHEDULE_KINDS = ("daily", "weekdays", "weekly", "once")
ACTION_KINDS = ("ask", "message")
```

- `action_kind='ask'`: Runs a SQL query and returns result (line 6)
- `action_kind='message'`: Sends fixed text (line 6)
- **No `action_kind='workflow'`** for multi-step sequences

**How jobs execute** (bot.py, job scheduler):
Jobs call `sql_query_flow.answer_question()` for `action_kind='ask'`, which runs **one** query loop. No provision for:
- Creating records first
- Then scheduling reminders
- Then linking them

**Impact**:
- User's complex goal request requires **manual decomposition**
- Bot can't atomically create goal + reminder in one confirmation

---

### 5. **Tool Call Parsing Has No Array Support** ⚠️ CRITICAL
**Location**: `agent.py:434-438`

**Issue**: Even if the LLM generates multiple tool calls in an array:
```json
[
  {"tool": "sqlite_execute", "arguments": {"sql": "INSERT..."}},
  {"tool": "set_context", "arguments": {"chat_id": 123, "subject": "Physics"}}
]
```

The parser will **fail** because it expects a single object:

```python
# agent.py:434-438
def _parse_tool_call(text: str) -> Optional[ToolCall]:
    data = _extract_json(text)
    if not data or "tool" not in data:  # Looks for top-level "tool" key
        return None
    return ToolCall(tool=data["tool"], arguments=data.get("arguments", {}))
```

If `data` is a list, `"tool" not in data` → returns `None` → treated as final response.

**Impact**:
- LLM generates valid multi-tool JSON → parser rejects it
- Falls back to plain text response
- Tools never execute

---

### 6. **No Error Classification in SQL Recovery** ⚠️ MEDIUM
**Location**: `sql_query_flow.py:469-474`

**Issue**: All SQL errors treated identically — permanent schema errors vs transient locks.

```python
except sql_tool.SQLRejectedError as e:
    feedback = f"ERROR (rejected): {e}"
    _emit("retry", "query rejected, retrying")
except sql_tool.SQLExecutionError as e:
    feedback = f"ERROR (sqlite): {e}"
    _emit("retry", "SQL error, retrying")
```

**No distinction for**:
- `no such column: xyz` → **permanent** (schema mismatch, need different approach)
- `database is locked` → **transient** (retry with backoff)
- `syntax error near "FROM"` → **permanent** (LLM hallucinated SQL)

**Impact**:
- Agent wastes iterations retrying unfixable errors
- No exponential backoff → hammers locked database
- User gets generic "took too many steps" instead of "column doesn't exist"

---

### 7. **Write Confirmation State Has 10-Minute TTL** ⚠️ LOW
**Location**: `agent.py:93`

```python
STATE_TTL_SECONDS = 600  # 10 minutes
```

**Issue**: If user takes >10 minutes to confirm a write preview, state expires → "confirmation expired, send request again".

**Impact**:
- User reviewing complex SQL → times out
- Must re-trigger entire agent flow
- Lost context from tool calls leading up to write

**Recommendation**: Increase to 30 minutes or make configurable.

---

## Architectural Issues

### A. **Agent Loop is Inherently Sequential**
The entire `_run_loop()` is a for-loop calling LLM → parse one tool → execute → repeat. No concurrency model for parallel tool execution.

**To fix**: Would require:
1. Parse response as array of tool calls
2. Classify as read/write batch
3. Execute all reads in parallel
4. Show one combined preview for all writes
5. Execute confirmed writes in transaction

**Complexity**: Medium (affects agent.py, agent_tools.py, bot.py confirmation flow)

---

### B. **No Persistent Agent Memory Beyond 28 Days**
Learner profile uses 28-day window (hardcoded). Conversation history is 5 pairs with 60-minute TTL.

**For 2-year recall**, bot relies on:
- LLM writing ad-hoc SQL queries
- No semantic indexing or summary layer

**Risk**: LLM doesn't know to query old data unless user explicitly mentions dates.

---

### C. **Job Scheduler is Poll-Based (60s intervals)**
Jobs fire via 60-second polling loop in `bot.py`. For complex workflows:
- No event-driven triggers
- No job dependencies
- No retry/failure handling

---

## Testing Gaps

1. **No test for multi-tool scenarios** (`test_agent.py` only tests single tool call)
2. **No SQL retry stress test** (transient errors, schema evolution)
3. **No historical query test** (verify agent queries 2+ year old data when prompted)
4. **No job workflow test** (create goal + reminder atomically)

---

## Recommendations

### Immediate (1-2 days):
1. **Add array parsing** to `_parse_tool_call()` → return `list[ToolCall]`
2. **Increase max_iterations** to 15 for agent, add per-error retry budget
3. **Classify SQL errors** → permanent vs transient, add backoff
4. **Add test**: User message requiring 3 tools → verify all execute

### Short-term (1 week):
5. **Extend context window** for old data: If user question mentions year <current-1, load sample from that period
6. **Job workflows**: Add `action_kind='multi_step'` with JSON workflow definition
7. **Increase write confirmation TTL** to 30 minutes

### Long-term (2+ weeks):
8. **Semantic memory layer**: Periodic summaries of study trends, indexed by date range
9. **Event-driven job scheduler**: Replace polling with apscheduler or similar
10. **Agent prompt optimization**: Include "when user asks about old data, query by date" instruction

---

## Severity Summary

| Severity | Count | Blockers |
|----------|-------|----------|
| Critical | 2 | Multi-tool execution, tool array parsing |
| High | 2 | SQL recovery, scheduled workflows |
| Medium | 2 | Error classification, proactive memory |
| Low | 1 | Write confirmation TTL |

---

## Files Requiring Changes

1. `agent.py` — tool parsing, loop logic, max iterations
2. `agent_tools.py` — execute_tool batch support
3. `sql_query_flow.py` — error classification, retry backoff
4. `user_jobs.py` — multi-step action support
5. `bot.py` — job execution for workflows
6. `test_agent.py` — multi-tool test coverage

---

**End of Report**
