# Files to Remove/Gitignore - Analysis

## ❌ UNNECESSARY FILES (Should be removed from git)

### 1. Live Test Files (Require Telethon - breaking CI)
**Proof:** Import error in CI - these tests need Telethon API credentials
- `test_live_e2e.py` - Full live integration test
- `test_live_telegram.py` - Telethon-based live test harness
- `test_handlers_live.py` - Live handler tests
- `test_jobs_live.py` - Live jobs tests
- `test_live_features.py` - Live feature tests
- `test_setup_ai_live.py` - Live setup tests
- `test_session.session` - Telethon credentials (security risk!)
- `live_e2e_*.log` - Test logs (already gitignored, but 4 files committed)

**Why remove:** Break CI, require manual credentials, not needed for production bot

### 2. Duplicate/Old Install Scripts
- `install.sh.1` - Backup/duplicate of install.sh
- `install.sh.2` - Another backup (if exists)
- `fedora.sh`, `fedora.sh.1`, `fedora.sh.2` - Fedora-specific (you're on Ubuntu VPS)

**Why remove:** Clutter, outdated, not used in production

### 3. Debug/Development Files
- `.botdiff.txt` - Temporary diff output
- `live_e2e_20260721_0251.log` - Old test log
- `live_e2e_20260721_0300.log` - Old test log  
- `live_e2e_20260721_0302.log` - Old test log
- `live_e2e_20260721_0310.log` - Old test log
- `.claude/` - Claude Code local settings (machine-specific)
- `.pytest_cache/` - Pytest cache (already gitignored)

**Why remove:** Temporary artifacts, machine-specific, no production value

### 4. Downloaded Dependencies (Should be reinstalled, not committed)
- `psutil-5.9.8.tar.gz` - Source tarball (belongs in pip cache)
- `fix-psutil.sh` - One-time Termux workaround

**Why remove:** Dependencies should be in requirements.txt, not committed as files

### 5. Media Files (Not code)
- `ElyOtto - SugarCrash! (Lyrics) ＂I'm on a sugar crash＂ [-7CG3ngSYls].webm` - Music file
- `ElyOtto - SugarCrash! (Official Video) [6uaq8GJJxAQ].webm` - Video file

**Why remove:** Not related to bot code, wasting repo space

### 6. JSON dumps (API exploration artifacts)
- `gemini_models.json` - API exploration artifact
- `groq_models.json` - API exploration artifact
- `or_models.json` - API exploration artifact
- `probe.js` - API probing script
- `model_probe.py` - API probing script

**Why remove:** One-time exploration, not needed in production

### 7. Documentation/PDFs
- `telegram-study-bot-gap-fixes.pdf` - Design doc (keep in Notion, not git)

**Why remove:** Binary file, should be in external docs

---

## ✅ KEEP THESE (Essential for production)

### Core Bot Files
- `bot.py` - Main bot
- `intent_parser.py`, `logging_flow.py`, `sql_query_flow.py` - Core logic
- `sync.py`, `session_context.py`, `draft_store.py` - Infrastructure
- `advisor.py`, `commitments.py`, `exam_readiness.py` - Features
- `formulas.py`, `notion_client_wrapper.py` - Integrations
- All `config/*.py` files
- All `llm/*.py` files (router)

### Working Tests (No Telethon dependency)
- `test_phase*.py` - Phase tests (offline)
- `test_formulas.py`, `test_advisor.py`, `test_commitments.py` - Unit tests
- `test_sql_*.py` - SQL tests
- `test_reset_service.py`, `test_exam_readiness.py` - Feature tests

### Config/Docs
- `.env.example` - Template for secrets
- `README.md`, `DEPLOYMENT_SUMMARY.md`, `TESTING_GUIDE.md` - Documentation
- `requirements.txt` - Dependencies
- `.gitignore` - Already good
- `run_bot.sh`, `backup_mirror.sh` - Utility scripts
- `study-bot.service` - Systemd config

---

## 📋 SUMMARY

**Remove from git:** 30+ files
- 7 live test files (breaking CI)
- 4 log files  
- 6 JSON/probe files
- 3 duplicate install scripts
- 2 media files
- 1 PDF
- 1 tarball
- Various debug artifacts

**Keep in git:** ~40 essential files
- Core bot code
- Offline tests
- Config/docs
- Utility scripts

**Expected result:** Clean repo, passing CI, ~50% smaller
