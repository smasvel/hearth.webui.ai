import database
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import memory_store


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


if __name__ == "__main__":
    unittest.main()
