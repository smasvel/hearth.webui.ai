"""Shared guidance for responses involving trading or medical decisions."""

_INSTRUCTION = (
    "For responses about trading, stocks, tickers, securities, market indexes (including "
    "the Nasdaq or S&P indexes), index funds, ETFs, or other investment topics, clearly "
    "state that the information is general and educational, not individualized financial "
    "or investment advice. Do not recommend specific transactions or personalized "
    "investment decisions. Encourage the user to consult an appropriately qualified "
    "financial professional for decisions about their circumstances. For medical topics, "
    "clearly state that the information is general and educational, not individualized "
    "medical advice; do not provide diagnoses or treatment plans, and encourage the user "
    "to consult an appropriately qualified medical professional. For urgent medical "
    "symptoms, direct them to emergency care."
)


def apply(messages: list[dict]) -> list[dict]:
    """Return copied messages with the shared advice disclaimer in the system context."""
    if not isinstance(messages, list):
        return messages
    copied = [dict(message) if isinstance(message, dict) else message for message in messages]
    for message in copied:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            if _INSTRUCTION not in message["content"]:
                message["content"] = f"{message['content']}\n\n{_INSTRUCTION}".strip()
            return copied
    return [{"role": "system", "content": _INSTRUCTION}, *copied]


def apply_body(body: dict) -> dict:
    """Copy an OpenAI chat body and add the guidance to its messages."""
    prepared = dict(body)
    messages = body.get("messages")
    if isinstance(messages, list):
        prepared["messages"] = apply(messages)
    return prepared
