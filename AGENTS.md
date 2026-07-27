# Agent notes

## Verify

```bash
python -m compileall -q .
pytest -q
```

CI (`.github/workflows/offline-tests.yml`) runs the same: `compileall` then `pytest -q` (offline, `-m "not live"`).

## Rich messages (Bot API 10.1)

- Module: `rich_message.py` — raw httpx calls for `sendRichMessage`, `sendRichMessageDraft`, and `editMessageText(rich_message=...)`.
- Wired into `bot.py` helpers `_reply_markdown` / `_edit_markdown` / `_send_markdown` and `agent_renderer.render`.
- Streaming pattern (`RichStream`): first → `sendRichMessage`; mid → `sendRichMessageDraft`; final → `editMessageText` with `rich_message`. Never bare `editMessageText` mid-stream.
- Disable: `RICH_MESSAGES=0`. Local Bot API: `TELEGRAM_API_BASE=http://...`.
