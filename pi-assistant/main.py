"""Entry point: wake word -> listen -> transcribe -> think -> act -> speak."""
import numpy as np
import sounddevice as sd
import yaml

from wake import WakeListener
from stt import Transcriber
from tts import Speaker
from brain import Brain
from devices import Home

SAMPLE_RATE = 16000
BLOCK = 1600  # 100 ms


def record_phrase(silence_sec=1.2, max_sec=12) -> np.ndarray:
    """Record the command until 1.2 s of silence (rough VAD)."""
    frames, silent = [], 0
    with sd.InputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK,
                        channels=1, dtype="int16") as mic:
        while len(frames) * BLOCK / SAMPLE_RATE < max_sec:
            chunk, _ = mic.read(BLOCK)
            frames.append(np.frombuffer(chunk, dtype=np.int16))
            if np.abs(frames[-1]).mean() < 250:      # tune for your mic
                silent += 1
                if silent >= int(silence_sec * 10):
                    break
            else:
                silent = 0
    return np.concatenate(frames)


def main():
    cfg = yaml.safe_load(open("config.yaml"))
    wake = WakeListener((cfg["wake_word"],))
    stt = Transcriber(cfg["stt"]["model"], cfg["stt"]["device"])
    speak = Speaker(cfg["tts"]["voice"])
    home = Home(cfg["homeassistant"]["url"], cfg["homeassistant"]["token"])
    brain = Brain(cfg)

    speak.say("Assistant ready.")
    while True:
        wake.wait()                       # 1. "Hey Jarvis"
        audio = record_phrase()           # 2. record the command
        text = stt.transcribe(audio)      # 3. speech -> text
        if not text:
            continue
        print("You:", text)

        result = brain.handle(text)       # 4. rules first, LLM fallback
        for action in result["actions"]:  # 5. act on the home
            try:
                home.call(action)
            except Exception:
                result["speech"] = "Sorry, that didn't work."

        q = result["query"]               # 6. answer questions
        if q and q.get("type") == "weather":
            from skills.weather import get_weather
            result["speech"] = get_weather()
        elif q and q.get("entity"):
            state = home.state(q["entity"])["state"]
            result["speech"] = q["reply"].format(state=state)

        speak.say(result["speech"])       # 7. talk back
        print("Assistant:", result["speech"])


if __name__ == "__main__":
    main()
