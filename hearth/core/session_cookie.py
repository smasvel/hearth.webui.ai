"""Per-instance Flask session cookie naming.

Browsers scope cookies by host, not port, so two Hearth installations opened as
``https://localhost:5443`` and ``https://localhost:5444`` would otherwise share one
``session`` cookie. Each login would overwrite the other, and because every installation
has its own signing key the overwritten value is rejected, so switching ports looks like
being logged out. Naming the cookie after the listen port keeps both logins independent
in the same browser. Hostnames still isolate instances on different machines, and the
signing key still isolates them cryptographically.
"""
import re


def session_cookie_name(port: int) -> str:
    """Return a fresh-generation cookie name, isolated by Hearth listen port."""
    digits = re.sub(r"\D", "", str(port)) or "0"
    # Bump this generation when replacing stale/expired session cookies so the browser
    # sends the new login independently of the cookie created by the previous app build.
    return f"hearth_session_{digits}_v2"
