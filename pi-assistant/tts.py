"""Text-to-speech with Piper, played on the speaker."""
import os
import subprocess
import tempfile


class Speaker:
    def __init__(self, voice="en_US-lessac-medium"):
        self.voice = voice

    def say(self, text: str):
        if not text:
            return
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            out = f.name
        try:
            subprocess.run(["piper", "--model", self.voice, "--output_file", out],
                           input=text.encode(), check=True)
            subprocess.run(["aplay", "-q", out], check=True)
        finally:
            os.unlink(out)
