"""Wake-word detection with openWakeWord - runs 100% on-device."""
import numpy as np
import sounddevice as sd
from openwakeword.model import Model

SAMPLE_RATE = 16000
CHUNK = 1280  # 80 ms frames


class WakeListener:
    def __init__(self, model_names=("hey_jarvis",)):
        self.model = Model(wakeword_models=list(model_names))
        self.stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, blocksize=CHUNK,
            dtype="int16", channels=1)

    def wait(self):
        """Block until the wake word is heard, then return."""
        self.stream.start()
        try:
            while True:
                chunk, _ = self.stream.read(CHUNK)
                scores = self.model.predict(np.frombuffer(chunk, dtype=np.int16))
                for name, score in scores.items():
                    if score > 0.5:
                        self.model.reset()
                        return
        finally:
            self.stream.stop()
