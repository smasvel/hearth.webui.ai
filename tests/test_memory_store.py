import database
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import memory_store
import chat_store


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "memory.db"
        self.db_patch = database.temporary_schema()
        self.db_patch.__enter__()
        memory_store.init_db()

    def tearDown(self):
        self.db_patch.__exit__(None, None, None)
        self.tempdir.cleanup()

    def test_search_skips_stale_vector_dimensions(self):
        memory_store.add_chunk("u", "old", "user", "stale", [1.0, 0.0])
        memory_store.add_chunk("u", "good", "user", "remember this", [1.0, 0.0, 0.0])

        hits = memory_store.search("u", [1.0, 0.0, 0.0])

        self.assertEqual([hit["content"] for hit in hits], ["remember this"])

    def test_duplicate_check_skips_stale_vector_dimensions(self):
        memory_store.add_chunk("u", "old", "user", "stale", [1.0, 0.0])
        self.assertFalse(memory_store.find_near_duplicate("u", "user", [1.0, 0.0, 0.0]))

    def test_search_keeps_embedding_backend_vectors_isolated(self):
        memory_store.add_chunk("u", "lm", "user", "Lemonade memory", [1.0, 0.0])
        memory_store.add_chunk("u", "kobold", "user", "Kobold memory", [0.0, 1.0],
                               embedding_backend="kobold")

        lemonade_hits = memory_store.search("u", [1.0, 0.0], embedding_backend="lemonade")
        kobold_hits = memory_store.search("u", [0.0, 1.0], embedding_backend="kobold")

        self.assertEqual([hit["content"] for hit in lemonade_hits], ["Lemonade memory"])
        self.assertEqual([hit["content"] for hit in kobold_hits], ["Kobold memory"])

    def test_duplicate_detection_is_scoped_to_embedding_backend(self):
        memory_store.add_chunk("u", "kobold", "user", "Kobold memory", [1.0, 0.0],
                               embedding_backend="kobold")

        self.assertFalse(memory_store.find_near_duplicate(
            "u", "user", [1.0, 0.0], embedding_backend="lemonade"))
        self.assertTrue(memory_store.find_near_duplicate(
            "u", "user", [1.0, 0.0], embedding_backend="kobold"))

    def test_latest_location_prefers_explicit_personal_location(self):
        memory_store.add_chunk(
            "u", "shops", "user",
            "Remember my Great Clips location 123 Main St, Raleigh NC", [1.0],
        )
        memory_store.add_chunk(
            "u", "profile", "user", "Set my current location as Exampleville ZZ", [1.0],
        )

        fact = memory_store.latest_location("u", exclude_conversation_id="other")

        self.assertEqual(fact["location"], "Exampleville ZZ")

    def test_latest_location_respects_conversation_exclusion(self):
        memory_store.add_chunk(
            "u", "current", "user", "My city is Exampleville ZZ", [1.0],
        )
        self.assertIsNone(memory_store.latest_location("u", "current"))

    def test_latest_location_recognizes_located_in_and_skips_prompt_placeholder(self):
        memory_store.add_chunk("u", "past", "user", "I'm located in Exampleville ZZ", [1.0])
        memory_store.add_chunk(
            "u", "newer", "user",
            "My location is [Clear, concise confirmation of your location]", [1.0],
        )

        fact = memory_store.latest_location("u")

        self.assertEqual(fact["location"], "Exampleville ZZ")

    def test_latest_location_reads_chat_history_when_embeddings_were_unavailable(self):
        chat_store.ensure_conversation("unindexed", "u", "koboldcpp/model", "I live in Exampleville ZZ")
        chat_store.add_message("unindexed", "user", "I live in Exampleville ZZ")

        fact = memory_store.latest_location("u")

        self.assertEqual(fact["location"], "Exampleville ZZ")

    def test_latest_name_prefers_explicit_personal_name(self):
        memory_store.add_chunk(
            "u", "old", "user", "My name is Maya Patel", [1.0],
        )
        memory_store.add_chunk(
            "u", "newer", "user", "Please call me Dr. S. Varma", [1.0],
        )

        fact = memory_store.latest_name("u", exclude_conversation_id="other")

        self.assertEqual(fact["name"], "Dr. S. Varma")

    def test_latest_name_ignores_non_personal_phrasings(self):
        memory_store.add_chunk(
            "u", "files", "user", "The file name is report_final.txt and my name is not important", [1.0],
        )
        memory_store.add_chunk(
            "u", "todo", "user", "Call me later about the appointment", [1.0],
        )

        self.assertIsNone(memory_store.latest_name("u"))

    def test_latest_name_respects_conversation_exclusion(self):
        memory_store.add_chunk(
            "u", "current", "user", "My name is Maya Patel", [1.0],
        )
        self.assertIsNone(memory_store.latest_name("u", "current"))


if __name__ == "__main__":
    unittest.main()
