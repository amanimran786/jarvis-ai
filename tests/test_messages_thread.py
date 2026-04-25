from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import messages_thread


def test_sent_message_is_available_by_contact_name_and_address():
    with TemporaryDirectory() as td, patch.object(messages_thread, "_THREADS_FILE", Path(td) / "threads.json"):
        messages_thread.record_sent("Farhan Butt", "+15105550179", "intro sent")

        assert [m["body"] for m in messages_thread.get_thread("Farhan Butt")] == ["intro sent"]
        assert [m["body"] for m in messages_thread.get_thread("+1 (510) 555-0179")] == ["intro sent"]


def test_incoming_relay_merges_with_existing_contact_thread():
    with TemporaryDirectory() as td, patch.object(messages_thread, "_THREADS_FILE", Path(td) / "threads.json"):
        messages_thread.record_sent("Farhan Butt", "+15105550179", "intro sent")
        messages_thread.record_incoming("Farhan Butt", "yo got it")

        formatted = messages_thread.format_thread_for_prompt("Farhan Butt")
        assert "Aman: intro sent" in formatted
        assert "Farhan Butt: yo got it" in formatted


def test_list_threads_deduplicates_contact_and_address_aliases():
    with TemporaryDirectory() as td, patch.object(messages_thread, "_THREADS_FILE", Path(td) / "threads.json"):
        messages_thread.record_sent("Farhan Butt", "+15105550179", "intro sent")

        threads = messages_thread.list_threads()
        assert len(threads) == 1
        assert threads[0]["contact"] == "Farhan Butt"
