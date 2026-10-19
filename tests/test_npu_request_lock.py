import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import npu_request_lock


class NPURequestLockTests(unittest.TestCase):
    def test_bounded_acquire_times_out_instead_of_hanging(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            npu_request_lock, "_LOCK_PATH", Path(directory) / "npu.lock"
        ):
            held = npu_request_lock.acquire()
            outcome = []

            def contend():
                started = time.monotonic()
                try:
                    npu_request_lock.acquire(timeout=0.15)
                except TimeoutError:
                    outcome.append(time.monotonic() - started)

            thread = threading.Thread(target=contend)
            thread.start()
            thread.join(timeout=1)
            npu_request_lock.release(held)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertLess(outcome[0], 0.8)


if __name__ == "__main__":
    unittest.main()
