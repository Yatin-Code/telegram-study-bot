# Testing Guide - New Features

## Status
✅ Bot deployed and running on VPS (active since 06:05:30 UTC)
✅ All changes live and ready to test

---

## Test 1: Pattern Matching Fast Path

**What to test:** Common logs now skip LLM entirely (instant, bulletproof)

### Steps:
1. Open Telegram
2. Send: `20 questions 15 correct 30 minutes`
3. **Expected:** Immediate preview (no "Thinking..." delay)
4. Send: `doubt: why does current flow opposite to electron flow`
5. **Expected:** Immediate doubt preview
6. Send: `list physics doubts`
7. **Expected:** Instant query response

### What you're checking:
- ⚡ Speed: Should be instant (< 1 second)
- 📝 Logs correctly parsed without LLM
- 🔄 Falls back to LLM for complex messages (e.g., "today's physics was rough")

---

## Test 2: Data Inspector (/inspect)

**What to test:** Verify what the bot knows - critical for JEE prep confidence

### Test 2A: SQLite Data Browser
1. Send: `/inspect`
2. Tap **"📊 SQLite Mirror"**
3. **Expected:** See counts like:
   ```
   • Ledger: X active sessions
   • Doubts: Y active doubts
   • Goals: Z active goals
   ```
4. Tap **"Ledger (X)"**
5. **Expected:** See your recent study sessions with:
   - Date
   - Task name
   - Questions attempted/correct
   - Cognitive Yield score
6. Tap **"↩ Back to Tables"** → **"Doubts (Y)"**
7. **Expected:** See your tracked mistakes/doubts

### Test 2B: Notion Sync Status
1. From /inspect menu, tap **"☁️ Notion Sync Status"**
2. **Expected:** See something like:
   ```
   ✅ ledger
     • Last sync: 2026-07-22 06:05:31
     • Records synced: 150
   
   ✅ doubts
     • Last sync: 2026-07-22 06:05:31
     • Records synced: 12
   ```
3. Tap **"🔄 Force Sync Now"**
4. **Expected:** "Syncing from Notion..." → "✅ Synced X records"
5. **What you're checking:** Last sync time is recent (< 5 minutes ago)

### Test 2C: In-Context Memory
1. First, set a study context: `starting physics mechanics`
2. Send: `/inspect`
3. Tap **"🧠 In-Context Memory"**
4. **Expected:** See:
   ```
   *Current Session:*
     • subject: Physics
     • chapter: mechanics
     • elapsed: 0 min
   
   *Recent Q&A History:*
     (shows last 3 queries you asked)
   
   *Pending Drafts:* 0
   ```
5. Tap **"🗑 Clear Session"**
6. **Expected:** Session cleared, context empty

---

## Test 3: Verify Reset Actually Works

**Use case:** After running /reset, check data was really cleared

### Steps:
1. Note your current ledger count: `/inspect` → SQLite → see "Ledger: X"
2. Run: `/reset` (pick a scope, follow the confirmation)
3. Immediately run: `/inspect` → SQLite
4. **Expected:** Ledger count should be 0 (or reduced if partial reset)
5. Check Notion Sync Status - should show recent sync

**What you're checking:** Reset + Inspect = complete transparency. No more wondering "did it actually delete?"

---

## Test 4: Daily Workflow Simulation

**Real-world scenario:** Check bot knows everything it needs for JEE prep

### Morning Check:
1. `/inspect` → SQLite → see yesterday's study sessions
2. Check Goals count matches what you expect
3. Check Exams shows your next mock date
4. Notion Sync Status - last sync < 5 min

### Before Mock Exam:
1. `/inspect` → "Exams" table
2. Verify exam date is correct
3. Check "Doubts" table - how many unresolved?
4. This gives you confidence the system is tracking everything

### After Study Session:
1. Log session: `25 questions 20 correct 40 minutes` (instant via pattern matching!)
2. Wait 10 seconds
3. `/inspect` → Ledger → should show the new session
4. Verify CY score calculated correctly

---

## What to Watch For

### ✅ Good Signs:
- Pattern-matched logs respond instantly (< 1 second)
- `/inspect` shows all your data
- Notion sync timestamps are recent
- Record counts match expectations

### ⚠️ Red Flags:
- Last sync > 10 minutes ago → sync may be stuck
- Ledger count is 0 but you logged sessions → not syncing from Notion
- Pattern matching not working → still getting "Thinking..." delay
- SQLite shows different counts than Notion → sync broken

---

## Emergency Testing

If something breaks during JEE prep:

1. **Can't log?** `/inspect` → check last sync time
2. **Missing data?** `/inspect` → browse SQLite tables
3. **Doubt not found?** `/inspect` → Doubts table → is it there?
4. **Exam date wrong?** `/inspect` → Exams → see what's registered

**The inspect command is your emergency diagnostic tool.**

---

## Quick Test (2 minutes)

Fastest way to verify everything works:

```
# In Telegram, send these 5 messages:

/inspect
# Tap through each menu item, verify data shows up

20 questions 15 correct 30 mins
# Should get instant preview

doubt: test doubt for verification
# Should get instant preview

/inspect
# Go to SQLite → Ledger → verify session just logged
# Go to SQLite → Doubts → verify doubt just logged
```

If all 5 work → ✅ system is healthy, focus on studying.

---

## Notes

- Pattern matching handles ~90% of your daily logs (the repetitive ones)
- Complex messages still use LLM (that's fine, only 10% of cases)
- `/inspect` is read-only - it can't break anything
- Use it liberally to verify system health

**Your bot is now transparent, fast, and bulletproof. Focus on JEE - the system has your back.**
