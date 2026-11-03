"""Offline smoke test - no mic, speaker, or Home Assistant needed."""
from brain import rule_intent

ROOMS = {
    "living room": ["light.living_room", "light.living_room_lamp", "switch.tv"],
    "bedroom":     ["light.bedroom", "fan.bedroom"],
    "kitchen":     ["light.kitchen", "switch.coffee_maker"],
    "default":     ["light.living_room", "climate.home"],
}


class FakeHome:
    def call(self, action):
        url = f"/api/services/{action['domain']}/{action['service']}"
        print(f"    HA POST {url}  target={action['target']}  data={action.get('data') or {}}")


def run():
    home = FakeHome()
    utterances = [
        "turn on the living room lights",
        "turn the bedroom light off",
        "turn on the fan in the bedroom",
        "turn on the living room tv",
        "switch off the kitchen coffee maker",
        "dim the living room lights",
        "set the bedroom lights to 40 percent",
        "set the temperature to 72 degrees",
        "turn everything off",
        "is the living room light on",
        "what's the weather",
        "tell me a joke",
    ]
    for u in utterances:
        print(f'You: "{u}"')
        r = rule_intent(u, ROOMS)
        for a in r["actions"]:
            home.call(a)
        q = r["query"]
        if q and q.get("type") == "weather":
            print("    -> weather skill runs, result is spoken")
        elif q:
            print(f"    HA GET /api/states/{q['entity']}")
            r["speech"] = q["reply"].format(state="on")   # pretend HA said 'on'
        if r["speech"]:
            print(f"Assistant: {r['speech']}")
        elif not (r["actions"] or r["query"]):
            print("    (no rule matched -> the LLM brain takes over)")
        print()


if __name__ == "__main__":
    run()
