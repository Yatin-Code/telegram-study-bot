# Command Migration Analysis

## Commands that CAN be replaced with natural language (agent handles these)

### ✅ High confidence - Simple data operations
1. `/newsession` → "Clear my session" / "Start fresh"
2. `/sync` → "Sync with Notion now"
3. `/goal` → "Show my goals" / "Add goal: Complete JEE syllabus"
4. `/remember` → "Remember that I study best in the morning"
5. `/forget` → "Forget my morning study preference"
6. `/exam` → "I have an exam on March 15" / "Show my exams"
7. `/backlog` → "Show my backlog" / "Add to backlog: Revise thermodynamics"
8. `/doubts` → "Show my doubts" / "What doubts do I have?"
9. `/weak` → "What are my weak topics?"
10. `/weekly` → "Show my weekly progress"
11. `/today` → "What's my plan for today?"
12. `/next` → "What should I do next?"
13. `/readiness` → "Am I ready for Physics exam?"
14. `/timetable` → "Show my timetable" / "Add Physics class at 9am Monday"
15. `/attempt` → "I tried wave optics doubt for 30 minutes but got stuck on interference"
16. `/dismissdoubt` → "Dismiss the wave optics doubt | Found answer in textbook"
17. `/resolvedoubt` → "I resolved the thermodynamics doubt"
18. `/reopendoubt` → "Reopen the kinematics doubt"
19. `/jobs` → "Show my scheduled jobs" / "Create a reminder for 5pm daily"
20. `/finish_exam` → "I finished the mock test, analyze it"
21. `/exam_summary` → "Exam stats: 90/100 marks, 45 attempted, 40 correct"
22. `/question_review` → "Question 12 was wrong because I forgot the formula"
23. `/complete_exam_analysis` → "Done reviewing all questions"

### ⚠️ Medium confidence - UI-heavy but possible
24. `/memory` → "Show my memory" / "What do you remember about me?"
25. `/settings` → "Change my timezone to IST" (but settings menu is interactive)
26. `/inspect` → "Inspect the database" / "Show me SQLite tables"
27. `/health` → "Show bot health" / "Is everything working?"

## Commands that SHOULD stay (special UI or critical functions)

### 🔒 Keep as commands
1. `/start` - Entry point, onboarding flow (special)
2. `/help` - Shows command catalog (meta)
3. `/setup` - Multi-step wizard with interactive UI (complex flow)
4. `/settings` - Interactive menu with inline keyboards (complex UI)
5. `/bug` - Bug reporting (special system function)
6. `/bugs` - Bug management (special system function)
7. `/reset` - Destructive operation requiring typed confirmation (safety-critical)

## Migration Strategy

### Phase 1: Update actions.py
Add natural language examples for all migratable commands so the agent knows it can handle them.

### Phase 2: Deprecate gradually
- Keep commands registered but add deprecation notice: "You can also say this in natural language: 'show my doubts'"
- Track usage of old commands
- Remove after 2-4 weeks if usage drops

### Phase 3: Update /help
Keep the 7 core commands in /help, add a section: "Or just ask me in plain English!"

## Testing Plan

Test that agent can handle:
1. "Show my doubts" → queries doubts table
2. "Add goal to finish thermodynamics" → inserts goal with preview
3. "What should I study next?" → calls next item logic
4. "I have an exam on April 1st" → creates exam record
5. "Sync my Notion data" → triggers sync
6. "What's my weak subject?" → queries ledger accuracy by subject
7. "Show weekly progress" → aggregates 7-day stats
8. "Clear my session" → calls set_context with nulls
