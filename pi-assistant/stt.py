"""Speech-to-text with faster-whisper (local, offline)."""
import numpy as np
from faster_whisper import WhisperModel


class Transcriber:
    def __init__(self, model_size="small.en", device="cpu"):
        self.model = WhisperModel(model_size, device=device, compute_type="int8")

    def transcribe(self, audio_int16: np.ndarray) -> str:
        float_audio = audio_int16.astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(
            float_audio, language="en", beam_size=1,
            condition_on_previous_text=False)
        return " ".join(s.text for s in segments).strip()
