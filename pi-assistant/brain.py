"""The brain: utterance -> {speech, actions, query}.

Two paths:
  1. rule_intent()  - regex parser, ~0 ms, offline, covers common commands
  2. ask_llm()      - Ollama with tool calling, handles everything else
main.py tries the rules first, falls back to the LLM.
"""
import json
import re

# object keywords -> Home Assistant domain
DOMAINS = [
    (("light", "lamp", "bulb"), "light"),
    (("ac", "air conditioner", "heater", "thermostat"), "climate"),
    (("fan",), "fan"),
    (("tv", "coffee maker", "plug", "socket", "blinds", "curtain", "garage"), "switch"),
]


def _domain_for(obj: str) -> str:
    for words, domain in DOMAINS:
        if any(w in obj for w in words):
            return domain
    return "light"


def _find_on_off(t):
    m = re.search(r"\b(?:turn|switch|put)\s+(?:the\s+)?(.+?)\s+(on|off)\b", t)
    if m:
        return m.group(1).strip(), m.group(2)
    m = re.search(r"\b(?:turn|switch|put)\s+(on|off)\s+(?:the\s+)?(.+?)\s*$", t)
    if m:
        return m.group(2).strip(), m.group(1)
    return None, None


def _no_match():
    return {"speech": "", "actions": [], "query": None}


def rule_intent(text: str, rooms: dict) -> dict:
    """Parse common home commands. rooms maps room name -> entity IDs."""
    t = " " + re.sub(r"[^a-z0-9% ]", " ", text.lower()) + " "
    room = next((r for r in rooms if r != "default" and r in t), "default")
    room_entities = rooms.get(room) or rooms.get("default", [])

    # 1) thermostat: "set the temperature to 72 degrees"
    if "temperature" in t or "degrees" in t or "thermostat" in t:
        m = re.search(r"\b(\d{2,3})\b", t)
        target = [e for e in room_entities if e.split(".")[0] == "climate"]
        if m and target:
            temp = int(m.group(1))
            return {"speech": f"Okay, setting the thermostat to {temp} degrees.",
                    "actions": [{"domain": "climate", "service": "set_temperature",
                                 "target": target, "data": {"temperature": temp}}],
                    "query": None}

    # 2) brightness: "set the bedroom lights to 40 percent"
    m = re.search(r"\b(\d{1,3})\s*(?:%|percent)\b", t)
    if m and any(w in t for w in ("light", "lamp", "bulb", "dim", "bright")):
        pct = min(int(m.group(1)), 100)
        target = [e for e in room_entities if e.split(".")[0] == "light"]
        if target:
            return {"speech": f"Okay, setting the {room} lights to {pct} percent.",
                    "actions": [{"domain": "light", "service": "turn_on",
                                 "target": target, "data": {"brightness_pct": pct}}],
                    "query": None}

    # 3) dim / brighten
    if " dim " in t or "brighten" in t:
        pct = 25 if " dim " in t else 75
        verb = "dimming" if " dim " in t else "brightening"
        target = [e for e in room_entities if e.split(".")[0] == "light"]
        if target:
            return {"speech": f"Okay, {verb} the {room} lights.",
                    "actions": [{"domain": "light", "service": "turn_on",
                                 "target": target, "data": {"brightness_pct": pct}}],
                    "query": None}

    # 4) on / off: "turn on the living room lights"
    obj, state = _find_on_off(t)
    if obj:
        if re.search(r"\b(everything|all)\b", obj):        # "turn everything off"
            by_domain = {}
            for ents in rooms.values():
                for e in ents:
                    by_domain.setdefault(e.split(".")[0], []).append(e)
            actions = [{"domain": d, "service": "turn_" + state,
                        "target": list(dict.fromkeys(es)), "data": {}}
                       for d, es in by_domain.items()]
            return {"speech": f"Okay, turning everything {state}.",
                    "actions": actions, "query": None}
        domain = _domain_for(obj)
        target = [e for e in room_entities if e.split(".")[0] == domain]
        if target:
            label = re.sub(r"\s+", " ",
                           re.sub(r"\b(?:in|at|to|the|please|now)\b", " ",
                                  obj.replace(room, ""))).strip()
            thing = label or domain
            return {"speech": f"Okay, turning the {room} {thing} {state}.",
                    "actions": [{"domain": domain, "service": "turn_" + state,
                                 "target": target, "data": {}}],
                    "query": None}

    # 5) status questions: "is the living room light on?"
    if re.search(r"\b(is|are|what|whats|status)\b", t) and re.search(r"\b(on|off)\b", t):
        target = [e for e in room_entities if e.split(".")[0] == "light"]
        if target:
            return {"speech": "", "actions": [],
                    "query": {"entity": target[0],
                              "reply": "The {} light is {{state}}.".format(room)}}

    # 6) weather skill
    if "weather" in t:
        return {"speech": "", "actions": [], "query": {"type": "weather"}}

    return _no_match()


# ---------------------------------------------------------------- LLM path
TOOLS = [
    {"type": "function", "function": {
        "name": "control_device",
        "description": "Control a smart home device",
        "parameters": {"type": "object", "properties": {
            "domain": {"type": "string",
                       "enum": ["light", "switch", "climate", "fan", "cover", "media_player"]},
            "service": {"type": "string",
                        "enum": ["turn_on", "turn_off", "toggle", "set_brightness", "set_temperature"]},
            "room": {"type": "string"},
            "value": {"type": "number"},
        }, "required": ["domain", "service", "room"]}}},
]


def _action_from_args(args, rooms):
    room = args.get("room", "default")
    domain, service = args["domain"], args["service"]
    target = [e for e in rooms.get(room, rooms.get("default", []))
              if e.split(".")[0] == domain]
    data = {}
    if service == "set_brightness":
        service, data = "turn_on", {"brightness_pct": int(args.get("value", 50))}
    elif service == "set_temperature":
        data = {"temperature": args.get("value", 22)}
    return {"domain": domain, "service": service, "target": target, "data": data}


def ask_llm(text: str, rooms: dict, model="qwen2.5:3b") -> dict:
    """Ask the local LLM (Ollama) with tool calling."""
    import ollama
    system = ("You are a home assistant. Reply with one short sentence. "
              "Use the control_device tool for any device command. "
              "Known rooms and devices: " + json.dumps(rooms))
    resp = ollama.chat(model=model,
                       messages=[{"role": "system", "content": system},
                                 {"role": "user", "content": text}],
                       tools=TOOLS)
    msg = resp["message"]
    actions = []
    for call in msg.get("tool_calls") or []:
        f = call["function"]
        args = f["arguments"] if isinstance(f["arguments"], dict) else json.loads(f["arguments"])
        actions.append(_action_from_args(args, rooms))
    return {"speech": msg.get("content") or "Done.", "actions": actions, "query": None}


class Brain:
    def __init__(self, cfg):
        self.rooms = cfg["rooms"]
        self.model = cfg["brain"]["model"]
        self.use_llm = cfg["brain"].get("backend") == "ollama"

    def handle(self, text: str) -> dict:
        result = rule_intent(text, self.rooms)          # fast path first
        if result["actions"] or result["query"] or result["speech"]:
            return result
        if self.use_llm:                                # smart path
            try:
                return ask_llm(text, self.rooms, self.model)
            except Exception:
                pass
        return {"speech": "I'm not sure how to do that yet.",
                "actions": [], "query": None}
