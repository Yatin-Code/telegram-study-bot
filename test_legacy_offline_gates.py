"""Make deterministic script-style checks part of the mandatory pytest gate."""

import test_briefing
import test_phase3_sync


def test_archived_sync_and_page_content_script_gate():
    assert test_phase3_sync.run()


def test_briefing_script_gate():
    assert test_briefing.run()
