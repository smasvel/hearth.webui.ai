import io
import time
import unittest
from unittest.mock import patch

import app


class VoiceTranscriptionRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.user_patch = patch.object(
            app.users_store,
            "get_user_by_id",
            return_value={"id": "probe", "username": "probe", "role": "admin"},
        )
        self.user_patch.start()
        self.addCleanup(self.user_patch.stop)
        with self.client.session_transaction() as session:
            session.update(
                logged_in=True,
                user_id="probe",
                username="probe",
                role="admin",
                last_activity=time.time(),
                login_csrf_token="voice-test-csrf",
            )

    def test_manual_and_hands_free_route_uses_lemonade_whisper(self):
        audio = io.BytesIO(b"sample audio")
        with patch.object(app.lemonade, "transcribe_audio", return_value="hello hearth") as whisper, \
             patch.object(app.fastflowlm, "transcribe_audio", side_effect=AssertionError("wrong transcription backend")):
            response = self.client.post(
                "/api/voice/transcribe",
                data={"audio": (audio, "recording.webm")},
                headers={"X-CSRF-Token": "voice-test-csrf"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["text"], "hello hearth")
        whisper.assert_called_once_with(b"sample audio", filename="recording.webm")

    def test_transcription_errors_return_actionable_backend_error(self):
        with patch.object(app.lemonade, "transcribe_audio", side_effect=app.lemonade.LemonadeError("Whisper unavailable")):
            response = self.client.post(
                "/api/voice/transcribe",
                data={"audio": (io.BytesIO(b"sample audio"), "recording.webm")},
                headers={"X-CSRF-Token": "voice-test-csrf"},
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("Whisper unavailable", response.json["error"])


if __name__ == "__main__":
    unittest.main()
