"""Live web search for chat, via the `ddgs` package (DuckDuckGo metasearch, no API key
needed — verified live before adopting it: `DDGS().text(query, max_results=N)` returns
real, current results as `[{title, href, body}, ...]`).

Auto-detected via should_search_web(), the same style as comfyui_client's image-request
detection — no manual per-message toggle anymore (there used to be one; removed once
this existed, since asking the user to flip a switch is exactly the kind of thing
automatic detection should make unnecessary). Deliberately biased toward *not*
searching, though: unlike missing an image request (which just gets a text reply
instead), searching when it wasn't needed adds a few seconds of latency and can inject
irrelevant results into an otherwise-fine answer from the model's own knowledge — so
this only fires on a concrete signal (a specific-topic query it already recognizes, an
explicit "look this up" style ask, or an explicit recency word), not on every question
that merely could benefit from current data.

For price-comparison / ticket-availability style queries, a plain search snippet
(DDGS's ~300-char body text) usually doesn't contain an actual number, which is what
was causing models to answer "I can't browse real-time inventory" even with search
results injected — they genuinely had no price in front of them. `enrich_with_price_hints`
fetches a handful of the top result pages directly and pulls price figures out of their
raw HTML (schema.org/JSON-LD product markup, itemprop="price", og:price meta tags, or
plain "$XX.XX" text as a last resort) so the model has real numbers to work with instead
of vague snippets.
"""

from __future__ import annotations

import concurrent.futures
import re
from urllib.parse import urlparse

import requests
from ddgs import DDGS

import page_scraper


class WebSearchError(RuntimeError):
    pass


_SEARCH_STOP_WORDS = {
    "a", "an", "and", "are", "at", "be", "browse", "by", "can", "check", "find",
    "for", "from", "get", "give", "how", "i", "in", "is", "it", "latest", "live",
    "look", "me", "of", "on", "online", "please", "search", "show", "the", "this",
    "to", "up", "use", "web", "what", "when", "where", "who", "with",
}
_AD_LABEL_RE = re.compile(r"^\s*(?:ad|ads|advertisement|sponsored)\s*[:\-|]", re.I)


def _search_terms(text: str) -> set[str]:
    terms = set()
    for token in re.findall(r"[a-z0-9][a-z0-9.+_-]{1,}", text.lower()):
        if token in _SEARCH_STOP_WORDS:
            continue
        terms.add(token)
        if len(token) > 4 and token.endswith("s"):
            terms.add(token[:-1])
    return terms


def filter_navigation_results(query: str, results: list[dict], limit: int | None = None) -> list[dict]:
    """Keep only organic results with an observable lexical connection to the query.

    This filter runs before Playwright receives URLs. A weak search result therefore
    remains a snippet at worst; Hearth does not navigate to it. Explicit ad metadata,
    sponsored labels, and known advertising destinations are always rejected.
    """
    query_terms = _search_terms(query)
    kept = []
    for result in results or []:
        href = str(result.get("href") or "")
        title = str(result.get("title") or "")
        body = str(result.get("body") or "")
        if result.get("is_ad") or result.get("sponsored"):
            continue
        if _AD_LABEL_RE.search(title) or _AD_LABEL_RE.search(body):
            continue
        if not page_scraper.is_allowed_navigation_url(href):
            continue
        host = (urlparse(href).hostname or "").replace("www.", "")
        candidate_terms = _search_terms(f"{title} {body} {host}")
        # With no meaningful terms (rare, e.g. "browse"), there is no defensible
        # relevance decision, so do not navigate automatically.
        if not query_terms or not (query_terms & candidate_terms):
            continue
        kept.append(result)
        if limit is not None and len(kept) >= limit:
            break
    return kept


def _research_queries(query: str) -> list[str]:
    """Produce a small, deterministic search plan without spending an LLM request."""
    cleaned = re.sub(
        r"\b(?:please|can you|could you|search|browse|look up|find out|online|on the web)\b",
        " ", query, flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?.")
    queries = [query.strip()]
    if cleaned and cleaned.lower() != query.strip().lower() and len(_search_terms(cleaned)) >= 2:
        queries.append(cleaned)
    q_lower = query.lower()
    if any(word in q_lower for word in ("latest", "current", "today", "recent", "newest")):
        import datetime
        fresh = f"{cleaned or query} {datetime.date.today().year}"
        if fresh.lower() not in {q.lower() for q in queries}:
            queries.append(fresh)
    return queries[:2]


def _rank_and_diversify(query: str, results: list[dict], limit: int) -> list[dict]:
    """Rank evidence by query coverage, title match, source quality, and domain diversity."""
    query_terms = _search_terms(query)
    scored = []
    seen_urls = set()
    for original_rank, result in enumerate(results):
        href = str(result.get("href") or "")
        parsed = urlparse(href)
        normalized_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
        if normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        title_terms = _search_terms(str(result.get("title") or ""))
        body_terms = _search_terms(str(result.get("body") or ""))
        host_terms = _search_terms(parsed.hostname or "")
        overlap = query_terms & (title_terms | body_terms | host_terms)
        title_overlap = query_terms & title_terms
        coverage = len(overlap) / max(len(query_terms), 1)
        host = (parsed.hostname or "").lower()
        authority = 2.0 if host.endswith((".gov", ".edu")) else 0.0
        if any(part in host for part in ("docs.", "developer.", "research.", "arxiv.org")):
            authority += 1.0
        score = (
            len(title_overlap) * 4.0
            + len(overlap) * 1.5
            + coverage * 5.0
            + authority
            + max(0.0, 2.0 - original_rank * 0.08)
        )
        enriched = dict(result)
        enriched["research_score"] = round(score, 3)
        scored.append((score, original_rank, host, enriched))

    scored.sort(key=lambda item: (-item[0], item[1]))
    selected = []
    per_domain: dict[str, int] = {}
    for _, _, host, result in scored:
        if per_domain.get(host, 0) >= 2:
            continue
        selected.append(result)
        per_domain[host] = per_domain.get(host, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def search(query: str, max_results: int = 6) -> list[dict]:
    try:
        planned_queries = _research_queries(query)
        candidates = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(planned_queries)) as pool:
            futures = [
                pool.submit(DDGS().text, planned, max_results=max_results * 3)
                for planned in planned_queries
            ]
            for future in futures:
                try:
                    candidates.extend(future.result(timeout=12) or [])
                except Exception:
                    continue
        filtered = filter_navigation_results(query, candidates)
        return _rank_and_diversify(query, filtered, max_results)
    except Exception as exc:  # ddgs raises its own exception hierarchy; any failure here
        # should degrade the same way memory recall does, not break the chat.
        raise WebSearchError(f"Web search failed: {exc}") from exc


_WEATHER_KEYWORDS = (
    "weather", "forecast", "temperature", "how hot", "how cold", "raining", "snowing",
    "radar", "storm", "storms", "thunderstorm", "clouds moving", "cloud cover",
)

# Codes from Open-Meteo's WMO weather-code table (their docs), collapsed to readable labels.
_WMO_WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def looks_like_weather_query(query: str) -> bool:
    q = query.lower()
    return any(kw in q for kw in _WEATHER_KEYWORDS)


_US_STATE_ABBR = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in",
    "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv",
    "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn",
    "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
}
_AMBIGUOUS_STATE_ABBR = {"in", "or", "hi", "ok", "me", "pa", "ma", "al", "co", "mo", "id", "de"}
_US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
}

_WEATHER_FILLER = {
    "whats", "what's", "what", "hows", "how's", "s", "is", "are", "the", "weather", "forecast", "temperature", "like",
    "today", "tomorrow", "now", "right", "currently", "current", "in", "at", "for", "how",
    "hot", "cold", "it", "raining", "snowing", "this", "week", "weekend", "tell", "me", "a", "an",
    "check", "radar", "see", "if", "or", "storm", "storms", "clouds", "cloud", "moving",
    "toward", "towards", "to", "coming", "approaching", "headed", "heading", "will",
    "there", "be", "any", "going", "gonna", "and", "of", "on", "near",
}


def extract_weather_location(query: str) -> str | None:
    """Pull the place name out of a weather question. Three strategies, in order:
    (1) a US state abbreviation or name found anywhere in the query, anchored on — the
    word right before it is almost always the city ("check radar ... moving to exampleville
    nc" -> "exampleville zz"), checked first since it's the least ambiguous signal regardless
    of how much conversational text surrounds it; (2) the LAST "in/at/for/near/around/to
    <place>" in the query, not the first, with trailing filler trimmed off the end
    ("storm moving to Exampleville" -> "Exampleville", "... for Austin TX this weekend" -> "Austin
    TX") - a verbose real question often contains several of these prepositions, and
    only the rightmost one actually introduces the place; (3) whatever non-filler words
    survive, but only if 3 or fewer remain — beyond that it's more likely leftover
    conversational noise than a real place name (confirmed live: a verbose real query,
    "check weather radar to see if the storm or clouds moving to exampleville zz", left nearly
    the whole sentence standing as the "location" before this cap and the two strategies
    above were added — which then silently failed to geocode and made the whole weather
    feature quietly do nothing on real-world phrasing, not just fail to be exact). None
    if nothing topical survives — guessing a location is worse than falling through to
    the general search path."""
    tokens = re.findall(r"[A-Za-z']+", query)
    lowered = [t.lower() for t in tokens]

    # Strategy 1: a state abbreviation/name anchor, checked first since it's the least
    # ambiguous signal available — wherever it appears, the word right before it is
    # almost always the city, regardless of how much conversational text surrounds it.
    # A handful of state abbreviations collide with ordinary English words (IN/"in",
    # OR/"or", HI/"hi", OK/"ok", ME/"me", PA/"pa", MA/"ma", AL/"al", CO/"co", MO/"mo",
    # ID/"id", DE/"de") — confirmed live this genuinely misfires without a guard: "the
    # weather in New York" matched "IN" (Indiana) at "in" before ever reaching the real
    # "New York" later in the sentence, and "...the storm or clouds..." matched "OR"
    # (Oregon) on the word "or". Real state abbreviations are almost always written
    # capitalized ("Exampleville ZZ", "Austin TX"), while these particular English words
    # almost never are mid-sentence, so the ambiguous set is only trusted when the
    # original token was actually written upper-case.
    for i, tok in enumerate(lowered):
        if tok in _US_STATE_ABBR and i > 0:
            if tok in _AMBIGUOUS_STATE_ABBR and tokens[i] != tokens[i].upper():
                continue
            return f"{tokens[i - 1]} {tokens[i]}"
        if i > 0 and f"{lowered[i - 1]} {tok}" in _US_STATE_NAMES and i > 1:
            candidate_words = [tokens[i - 2], tokens[i - 1], tokens[i]]
            if candidate_words[0].lower() in _WEATHER_FILLER:
                candidate_words.pop(0)
            return " ".join(candidate_words)

    # Strategy 2: the LAST "in/at/for/near/around/to" in the query, not the first — a
    # verbose real question ("check weather radar to see if the storm ... moving to
    # exampleville") often contains several of these words, and only the rightmost one is
    # actually introducing the place. Trailing filler after the location (e.g. "... TX
    # this weekend") is trimmed word-by-word from the end.
    prep_positions = [i for i, w in enumerate(lowered) if w in ("in", "at", "for", "near", "around", "to")]
    if prep_positions:
        candidate_words = tokens[prep_positions[-1] + 1:]
        while candidate_words and candidate_words[-1].lower() in _WEATHER_FILLER:
            candidate_words.pop()
        candidate = " ".join(candidate_words)
        if candidate and candidate.lower() not in _WEATHER_FILLER:
            return candidate

    # Strategy 3: whatever non-filler words survive, but only if 3 or fewer remain -
    # beyond that it's more likely leftover conversational noise than a real place name.
    words = [w for w in tokens if w.lower() not in _WEATHER_FILLER]
    return " ".join(words) if 0 < len(words) <= 3 else None


def _geocode(location: str, timeout: float = 8.0) -> dict | None:
    """Resolves a place name to lat/lon/timezone via Open-Meteo's free geocoding API,
    shared by fetch_weather and fetch_world_time. Its matcher is picky in a way that
    genuinely broke both features for the single most common way people phrase a US
    location — confirmed live it returns *zero* results for "Exampleville ZZ", "Austin TX",
    "Miami FL", "Portland OR", even "Portland Oregon" (full state name, not just the
    abbreviation) — only the bare city name ("Exampleville", "Portland") matches. Real user
    report traced to exactly this: a weather query for a "city, state" location silently
    fell through to generic web search instead, which produced a mismatched/confusing
    context (search-engine snippets standing in for real weather data) — plausibly what
    triggered a downstream model repetition loop. Retries with the trailing state token
    (2-letter code or full name) stripped before giving up, rather than failing outright."""
    try:
        resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1},
            timeout=timeout,
        ).json()
        hits = resp.get("results") or []
        if hits:
            return hits[0]
    except Exception:
        return None

    words = location.split()
    stripped = None
    if len(words) >= 2 and words[-1].lower() in _US_STATE_ABBR:
        stripped = " ".join(words[:-1])
    elif len(words) >= 2 and words[-1].lower() in _US_STATE_NAMES:
        stripped = " ".join(words[:-1])
    elif len(words) >= 3 and " ".join(words[-2:]).lower() in _US_STATE_NAMES:
        stripped = " ".join(words[:-2])
    if not stripped:
        return None
    try:
        resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": stripped, "count": 1},
            timeout=timeout,
        ).json()
        hits = resp.get("results") or []
        return hits[0] if hits else None
    except Exception:
        return None


def fetch_weather(location: str) -> str | None:
    """Live conditions + 3-day forecast via Open-Meteo (free, no API key — consistent
    with this app's no-cloud-API-keys rule; same reason ddgs was picked for search).
    Two-step: _geocode() resolves the place name, then their forecast API returns
    actual numbers. Returns a formatted context block, or None on any failure (fail-soft,
    same contract as everything else in this module) — callers should fall back to the
    general search path rather than reporting an error."""
    try:
        place = _geocode(location)
        if not place:
            return None
        resolved = ", ".join(
            str(p) for p in [place.get("name"), place.get("admin1"), place.get("country")] if p
        )
        w = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
                "hourly": "precipitation_probability,precipitation,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "forecast_days": 3,
                "forecast_hours": 12,
                "timezone": "auto",
            },
            timeout=8,
        ).json()
        cur = w.get("current") or {}
        hourly = w.get("hourly") or {}
        daily = w.get("daily") or {}
        cond = _WMO_WEATHER_CODES.get(cur.get("weather_code"), "unknown conditions")
        cur_precip = cur.get("precipitation")
        lines = [
            f'Live weather for {resolved} (from Open-Meteo, fetched just now, local time {cur.get("time", "?")}):',
            f'- Now: {cur.get("temperature_2m")}°F ({cond}), feels like {cur.get("apparent_temperature")}°F, '
            f'humidity {cur.get("relative_humidity_2m")}%, wind {cur.get("wind_speed_10m")} mph, '
            f'currently {("precipitating, " + str(cur_precip) + " in/hr") if cur_precip else "no precipitation"}',
        ]
        # Next-12-hours precipitation timing — the closest real substitute this app has
        # for "reading a live radar map": Open-Meteo has no radar imagery product, but its
        # hourly precipitation probability/amount answers the actual underlying question
        # ("is rain/a storm about to arrive here") in text form. Only the hours with a
        # meaningful chance are surfaced, not all 12, so this doesn't bury the current
        # conditions above under a wall of "0% chance" lines.
        hourly_times = hourly.get("time") or []
        notable_hours = []
        for i, t in enumerate(hourly_times):
            prob = (hourly.get("precipitation_probability") or [None] * len(hourly_times))[i]
            amt = (hourly.get("precipitation") or [None] * len(hourly_times))[i]
            code = (hourly.get("weather_code") or [None] * len(hourly_times))[i]
            if prob is not None and prob >= 30:
                hour_label = t.split("T")[-1] if "T" in t else t
                desc = _WMO_WEATHER_CODES.get(code, "")
                notable_hours.append(f"{hour_label} ({prob}%{', ' + desc if desc else ''})")
        if notable_hours:
            lines.append(
                "- Precipitation expected in the next 12 hours at: " + ", ".join(notable_hours)
            )
        else:
            lines.append("- No meaningful precipitation chance (≥30%) in the next 12 hours.")
        for i, day in enumerate(daily.get("time") or []):
            day_cond = _WMO_WEATHER_CODES.get((daily.get("weather_code") or [None] * 3)[i], "")
            lines.append(
                f'- {day}: high {daily["temperature_2m_max"][i]}°F / low {daily["temperature_2m_min"][i]}°F'
                + (f", {day_cond}" if day_cond else "")
                + f', precipitation chance {daily["precipitation_probability_max"][i]}%'
            )
        lines.append(
            "You have live weather data above, fetched moments ago, including current conditions "
            "and hour-by-hour precipitation timing for the next 12 hours - use that to answer "
            "questions about incoming rain/storms. There is no live radar *imagery* available (no "
            "visual map), so if the user specifically wants to see a radar map, say so and point "
            "them to radar.weather.gov or a weather app - but still answer what you can (timing, "
            "chance, current conditions) from the real data above rather than deflecting entirely. "
            "Do not say you lack real-time access outright — answer directly from it, in both °F "
            "and °C if helpful."
        )
        return "\n".join(lines)
    except Exception:
        return None


_TIME_KEYWORDS = ("what time", "current time", "local time", "time in", "time zone", "timezone", "time right now")


def looks_like_time_query(query: str) -> bool:
    return any(kw in query.lower() for kw in _TIME_KEYWORDS)


_TIME_FILLER = {
    "what", "whats", "what's", "time", "is", "it", "in", "at", "the", "current", "local",
    "right", "now", "zone", "timezone", "tell", "me", "a", "an", "today",
}


def extract_time_location(query: str) -> str | None:
    words = [w for w in re.findall(r"[A-Za-z']+", query) if w.lower() not in _TIME_FILLER]
    return " ".join(words) if words else None


def fetch_world_time(location: str) -> str | None:
    """Current local time for a place, with no time API at all: Open-Meteo's geocoder
    (already used for weather) returns each place's IANA timezone id, and Python's own
    zoneinfo does the rest locally. Search snippets can never answer this one — every
    world-clock site renders the actual time via JS, so the crawled text is only ever
    site descriptions (confirmed live, same failure class as weather/prices). Fail-soft:
    None on any failure, caller falls through to the general search path."""
    try:
        from zoneinfo import ZoneInfo

        place = _geocode(location)
        if not place:
            return None
        tz_name = place.get("timezone")
        if not tz_name:
            return None
        resolved = ", ".join(
            str(p) for p in [place.get("name"), place.get("admin1"), place.get("country")] if p
        )
        import datetime as _dt

        now_there = _dt.datetime.now(ZoneInfo(tz_name))
        now_here = _dt.datetime.now().astimezone()
        return (
            f"Live world-clock data (computed just now from the IANA timezone database):\n"
            f"- Current local time in {resolved}: {now_there.strftime('%A, %B %d, %Y at %I:%M %p %Z')} "
            f"(timezone {tz_name}, UTC{now_there.strftime('%z')[:3]}:{now_there.strftime('%z')[3:]})\n"
            f"- For reference, this server's local time is {now_here.strftime('%I:%M %p %Z')}.\n"
            f"You have the exact current time above — answer directly from it, do not say you "
            f"lack real-time access."
        )
    except Exception:
        return None


# ISO codes Frankfurter (ECB reference rates) actually supports, plus common spoken names.
_CURRENCY_CODES = {
    "USD", "EUR", "GBP", "INR", "JPY", "CNY", "AUD", "CAD", "CHF", "HKD", "SGD", "KRW",
    "MXN", "BRL", "ZAR", "SEK", "NOK", "DKK", "PLN", "NZD", "THB", "TRY", "MYR", "PHP",
    "IDR", "HUF", "CZK", "ILS", "RON", "BGN", "ISK",
}
_CURRENCY_NAMES = {
    "dollar": "USD", "dollars": "USD", "buck": "USD", "bucks": "USD",
    "euro": "EUR", "euros": "EUR",
    "pound": "GBP", "pounds": "GBP", "sterling": "GBP",
    "rupee": "INR", "rupees": "INR",
    "yen": "JPY", "yuan": "CNY", "renminbi": "CNY",
    "won": "KRW", "peso": "MXN", "pesos": "MXN",
    "real": "BRL", "reais": "BRL", "franc": "CHF", "francs": "CHF",
    "rand": "ZAR", "baht": "THB", "lira": "TRY", "ringgit": "MYR", "rupiah": "IDR",
    "shekel": "ILS", "shekels": "ILS", "krona": "SEK", "krone": "NOK",
}
_RATE_KEYWORDS = ("exchange rate", "conversion rate", "convert", "how much is", "how many", "to")


def extract_currency_pair(query: str) -> tuple[str, str, float] | None:
    """(from_code, to_code, amount) if the query names two distinct known currencies —
    by ISO code ("USD to INR") or spoken name ("dollars to rupees") — else None.
    Amount defaults to 1; a leading number ("convert 100 usd to inr") is picked up."""
    found: list[str] = []
    for token in re.findall(r"[A-Za-z]+", query):
        code = token.upper() if token.upper() in _CURRENCY_CODES else _CURRENCY_NAMES.get(token.lower())
        if code and (not found or found[-1] != code):
            found.append(code)
    if len(found) < 2 or found[0] == found[1]:
        return None
    amount = 1.0
    m = re.search(r"\b(\d+(?:,\d{3})*(?:\.\d+)?)\b", query)
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return found[0], found[1], amount


def looks_like_currency_query(query: str) -> bool:
    q = query.lower()
    return extract_currency_pair(query) is not None and any(
        kw in q for kw in ("rate", "convert", "exchange", "how much", "how many", "worth")
    )


def fetch_exchange_rate(from_code: str, to_code: str, amount: float = 1.0) -> str | None:
    """Dated official rate via Frankfurter (ECB reference rates, free, no API key —
    same no-cloud-keys rule as ddgs/Open-Meteo). Search snippets for rate queries are
    unreliable for the actual number (confirmed live: a full result set of converter
    sites with no rate figure in any snippet). ECB rates update once per business day,
    which the injected context states explicitly via the as-of date rather than
    pretending to be tick-level data. Fail-soft: None on any failure."""
    try:
        resp = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": from_code, "to": to_code, "amount": amount},
            timeout=8,
        )
        data = resp.json()
        rate_value = (data.get("rates") or {}).get(to_code)
        if rate_value is None:
            return None
        as_of = data.get("date", "unknown date")
        unit_note = ""
        if amount not in (0, 1.0):
            unit_note = f" (1 {from_code} = {round(rate_value / amount, 4)} {to_code})"
        return (
            f"Live exchange-rate data (European Central Bank reference rate via Frankfurter, "
            f"as of {as_of} — ECB rates update once per business day):\n"
            f"- {amount:g} {from_code} = {rate_value} {to_code}{unit_note}\n"
            f"You have an official, dated exchange rate above — answer directly from it, noting "
            f"the as-of date, rather than saying you lack real-time access. Mention that "
            f"consumer/bank rates will differ slightly from this interbank reference rate."
        )
    except Exception:
        return None


_NEWS_KEYWORDS = ("news", "headline", "headlines", "current events", "what's happening", "whats happening")


def looks_like_news_query(query: str) -> bool:
    """Gate for routing to search_news() instead of the plain text search — a news-flavored
    question needs actual dated articles, not the generic web result a plain search gives
    (confirmed live: DDGS().text("What's today's news") returns homepage links like
    cbsnews.com/nbcnews.com with generic site-description snippets, no actual headlines —
    nothing for the model to summarize, which is what caused the reported failure)."""
    q = query.lower()
    return any(kw in q for kw in _NEWS_KEYWORDS)


_NEWS_FILLER_WORDS = {
    "whats", "what's", "what", "is", "the", "todays", "today's", "today", "latest",
    "current", "news", "headline", "headlines", "on", "in", "happening", "right", "now",
    "give", "me", "tell", "about", "can", "you", "show", "any", "for", "of", "a", "an",
    "up", "with", "going",
}

_NEWS_TOPIC_SEEDS = ("world news", "business news", "technology news", "us news")


def _is_generic_news_request(query: str) -> bool:
    """True when the query is just asking for 'the news' with no specific subject (e.g.
    "what's today's news") — as opposed to a topic-specific one ("news about the stock
    market"). Strips common question filler and checks whether anything topical survives."""
    words = re.findall(r"[a-z']+", query.lower())
    return not any(w not in _NEWS_FILLER_WORDS for w in words)


def search_news(query: str, max_results: int = 8) -> list[dict]:
    """News search via DDGS().news() with a 1-day recency filter — actual dated articles,
    not homepages. For a generic "what's the news" request (no specific topic), searching
    the literal question text performs badly (confirmed live: 1 stale, irrelevant result,
    since ddgs treats it as literal keywords rather than "give me a digest"). Instead this
    builds a front-page-style digest by querying a handful of broad topic seeds and merging
    the results, deduplicated by URL — much closer to what a human glancing at an actual
    news homepage would see. A topic-specific query ("news about the stock market") is
    searched directly since the literal text is actually meaningful there."""
    if _is_generic_news_request(query):
        seen_urls: set[str] = set()
        combined: list[dict] = []
        per_topic = max(2, max_results // len(_NEWS_TOPIC_SEEDS))
        any_success = False
        for seed in _NEWS_TOPIC_SEEDS:
            try:
                for r in DDGS().news(seed, max_results=per_topic, timelimit="d"):
                    any_success = True
                    url = r.get("url")
                    if url and url in seen_urls:
                        continue
                    if url:
                        seen_urls.add(url)
                    combined.append(r)
            except Exception:
                continue
        if not any_success:
            raise WebSearchError("News search failed: all topic categories failed")
        return combined[:max_results]
    try:
        return DDGS().news(query, max_results=max_results, timelimit="d")
    except Exception as exc:
        raise WebSearchError(f"News search failed: {exc}") from exc


def format_news_for_context(query: str, results: list[dict]) -> str | None:
    """Same injection pattern as format_for_context — hedged reference material, not
    ground truth. Includes each article's date, since "is this actually current" is the
    whole point of a news query and a bare title/snippet doesn't convey that."""
    if not results:
        return None
    lines = [f"Live news search results for \"{query}\" (fetched just now, may be incomplete):"]
    for r in results:
        title = r.get("title") or r.get("url") or "Untitled"
        url = r.get("url") or r.get("href") or ""
        date = (r.get("date") or "")[:10]
        source = r.get("source") or ""
        body = (r.get("full_text") or r.get("body") or "").strip()
        prefix = f"- [{date}]" if date else "-"
        source_part = f" ({source})" if source else ""
        lines.append(f"{prefix} {title}{source_part} ({url}): {body[:2200]}")
    lines.append(
        "You have live, dated news results above, fetched just now. Do not say you lack "
        "real-time access or cannot browse the internet — summarize what's actually there, "
        "noting the dates, and cite source links. If the results only cover some topics, say "
        "so rather than inventing coverage of topics that aren't in the data. Tell the user "
        "explicitly that this comes from a live online news search performed just now."
    )
    return "\n".join(lines)


_SHOPPING_KEYWORDS = (
    "price", "prices", "cost", "costs", "buy", "cheap", "cheapest", "deal", "deals",
    "discount", "sale", "ticket", "tickets", "availability", "available", "in stock",
    "book", "booking", "reservation", "compare",
)


def looks_like_shopping_query(query: str) -> bool:
    """Heuristic gate for the extra page-fetching work in enrich_with_price_hints —
    only worth the added latency (a few seconds) when the question is actually about
    price or availability, not every web-search toggle use."""
    q = query.lower()
    return any(kw in q for kw in _SHOPPING_KEYWORDS)


_PRICE_PATTERNS = [
    re.compile(r'"price"\s*:\s*"?\$?([\d,]+\.\d{2})"?', re.I),
    re.compile(r'"lowPrice"\s*:\s*"?\$?([\d,]+\.\d{2})"?', re.I),
    re.compile(r'itemprop=["\']price["\'][^>]*content=["\']\$?([\d,]+(?:\.\d{1,2})?)["\']', re.I),
    re.compile(r'property=["\']product:price:amount["\'][^>]*content=["\']\$?([\d,]+(?:\.\d{1,2})?)["\']', re.I),
    re.compile(r'\$\s?(\d{1,3}(?:,\d{3})*\.\d{2})\b'),  # plain visible "$XX.XX" as last resort
]


def _extract_prices(html: str) -> list[str]:
    hits: list[str] = []
    seen_values: set[float] = set()
    for pattern in _PRICE_PATTERNS:
        for m in pattern.finditer(html):
            raw_price = m.group(1)
            try:
                value = float(raw_price.replace(",", ""))
            except ValueError:
                continue
            if not (0.5 <= value <= 100_000) or value in seen_values:
                continue
            seen_values.add(value)
            hits.append(f"${raw_price}")
            if len(hits) >= 3:
                break
        if len(hits) >= 3:
            break
    return hits


def fetch_price_hints(url: str, timeout: float = 4.0, max_bytes: int = 400_000) -> list[str]:
    """Best-effort: pull likely price figures out of a result page's raw HTML via a plain
    GET. Many retail sites render the real price client-side via JS that a plain GET never
    sees, so an empty result here means "not found in static markup", not "no price
    exists" - enrich_with_price_hints() below tries a real rendered browser first via
    page_scraper and only falls back to this for the (rare) case Playwright isn't
    available. Fails soft: any network/parse error just yields no hints, same contract as
    the rest of this module."""
    if not url:
        return []
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ai-webui-research/1.0)"},
            stream=True,
        )
        chunks: list[bytes] = []
        total = 0
        for chunk in resp.iter_content(chunk_size=8192):
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                break
        html = b"".join(chunks).decode(resp.encoding or "utf-8", errors="ignore")
    except Exception:
        return []
    return _extract_prices(html)


def enrich_with_price_hints(results: list[dict], max_fetch: int = 4, screenshot_dir=None, query: str | None = None) -> list[dict]:
    """Renders a few of the top result pages in a real headless browser (page_scraper,
    Playwright/Chromium) and pulls both price figures and readable article text out of
    the fully-rendered page - closing the exact JS-rendering gap fetch_price_hints()'s
    plain GET can't see (confirmed live: schema.org/JSON-LD price markup and
    itemprop="price" tags are frequently injected client-side, invisible to a raw GET but
    present once the page actually runs). Falls back to the old plain-GET path
    (fetch_price_hints, concurrent via threads since plain `requests` calls are
    thread-safe) when Playwright isn't installed, so this still works on a deployment
    that skipped `playwright install chromium`. Returns new dicts (originals untouched)
    with added 'price_hints' and 'full_text' keys."""
    targets = results[:max_fetch]
    if not targets:
        return results
    urls = [r.get("href", "") for r in targets if r.get("href")]

    if page_scraper.available():
        pages = page_scraper.fetch_pages(
            urls, timeout=8.0, screenshot_dir=screenshot_dir,
            history_query=query, history_source="shopping",
        )
        enriched = []
        for r in results:
            r = dict(r)
            page = pages.get(r.get("href", ""))
            if page:
                r["price_hints"] = _extract_prices(page["html"])
                if page["text"]:
                    r["full_text"] = page["text"][:3500]
                if page.get("screenshot"):
                    r["screenshot_file"] = page["screenshot"]
                if page.get("images"):
                    r["image_files"] = page["images"]
            enriched.append(r)
        return enriched

    hints_by_url: dict[str, list[str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(urls), 1)) as pool:
        future_to_url = {pool.submit(fetch_price_hints, url): url for url in urls}
        for fut, url in future_to_url.items():
            try:
                hints_by_url[url] = fut.result(timeout=6)
            except Exception:
                hints_by_url[url] = []
    return [{**r, "price_hints": hints_by_url.get(r.get("href", ""), [])} for r in results]


_URL_RE = re.compile(r'https?://[^\s<>()\[\]"\']+')


def extract_urls(query: str | None) -> list[str]:
    """Raw URLs pasted directly into a chat message - e.g. a bare "https://example.com/"
    with no other words, or "browse https://example.com/ and summarize". These are a
    stronger, more precise browse signal than any keyword (they name an exact page, not
    just an intent), and critically are NOT a good DDGS search query - confirmed live
    that a message which is just a URL matched none of should_search_web()'s keyword
    lists, so no context was injected at all and the model fell back to its trained "I
    can't browse" refusal even though the whole point was to fetch that exact page."""
    if not query:
        return []
    return [u.rstrip(".,;:!?)") for u in _URL_RE.findall(query)]


def fetch_urls_directly(urls: list[str], max_chars: int = 3000, screenshot_dir=None, query: str | None = None) -> list[dict]:
    """Fetches URLs pasted directly into the message (see extract_urls) via a real
    headless browser and returns them in the same {title, href, body, full_text} shape
    search() results use, so format_for_context renders them identically. Bypasses DDGS
    entirely - a raw URL already names the exact page, searching for it as a query string
    would just return unrelated results. A no-op-per-url (empty full_text) when
    Playwright isn't installed or a given page fails to render, same fail-soft contract
    as enrich_with_full_text; callers should check for any non-empty full_text before
    treating this as a successful browse."""
    urls = [url for url in urls if page_scraper.is_allowed_navigation_url(url)]
    pages = (
        page_scraper.fetch_pages(
            urls, timeout=8.0, screenshot_dir=screenshot_dir,
            history_query=query, history_source="pasted-url",
        )
        if page_scraper.available()
        else {}
    )
    results = []
    for url in urls:
        page = pages.get(url)
        text = page["text"][:max_chars] if page and page.get("text") else ""
        results.append({
            "title": url, "href": url, "body": text[:300], "full_text": text,
            "screenshot_file": (page or {}).get("screenshot"),
            "image_files": (page or {}).get("images") or [],
        })
    return results


def enrich_with_full_text(results: list[dict], max_fetch: int = 3, max_chars: int = 3500, screenshot_dir=None, query: str | None = None, source: str = "search") -> list[dict]:
    """Renders the top few result pages in a real headless browser and replaces their
    thin ~300-char DDGS snippet with real, full article text for the model to work
    from - the general-research counterpart to enrich_with_price_hints above. A no-op
    (returns results unchanged) when Playwright isn't installed/available, so a plain
    snippet-only search is still what callers get on a deployment without it - never
    raises, same fail-soft contract as the rest of this module."""
    targets = results[:max_fetch]
    if not targets or not page_scraper.available():
        return results
    urls = [r.get("href", "") for r in targets if r.get("href")]
    pages = page_scraper.fetch_pages(
        urls, timeout=8.0, screenshot_dir=screenshot_dir,
        history_query=query, history_source=source,
    )
    if not pages:
        return results
    enriched = []
    for r in results:
        r = dict(r)
        page = pages.get(r.get("href", ""))
        if page and page["text"]:
            r["full_text"] = page["text"][:max_chars]
        if page and page.get("screenshot"):
            r["screenshot_file"] = page["screenshot"]
        if page and page.get("images"):
            r["image_files"] = page["images"]
        enriched.append(r)
    return enriched


def format_for_context(query: str, results: list[dict]) -> str | None:
    """Same shape as _recall_memory's output in app.py — a labelled block meant to be
    folded into the system message, hedged so the model treats it as reference material
    it can disagree with or ignore, not ground truth. When any result carries extracted
    price_hints, an explicit instruction is appended telling the model it does have live
    data and shouldn't deflect with "I can't check current prices" — that reflexive
    disclaimer is exactly the behavior this enrichment exists to head off. When a result
    carries full_text (a real Playwright-rendered page, from enrich_with_full_text or
    enrich_with_price_hints), that's used in place of the ~300-char DDGS snippet — real
    article content the model can actually work with instead of a homepage-style blurb."""
    if not results:
        return None
    lines = [f"Live web search results for \"{query}\" (may be incomplete or out of date):"]
    any_price_hints = False
    any_full_text = False
    for r in results:
        title = r.get("title") or r.get("href") or "Untitled"
        href = r.get("href") or ""
        full_text = (r.get("full_text") or "").strip()
        if full_text:
            any_full_text = True
            # 1500 → 2200 (2026-07-23, "improve the details" request). Balance point:
            # Lemonade's daily-driver models load at 32k context where this is nothing,
            # while the vLLM smoke-test model at 4096 gets tight with 4-5 results —
            # worth it, since thin excerpts were producing thin answers.
            text, cap = full_text, 2200
        else:
            text, cap = (r.get("body") or "").strip(), 300
        line = f"- {title} ({href}): {text[:cap]}"
        price_hints = r.get("price_hints") or []
        if price_hints:
            any_price_hints = True
            line += f" | Prices found on this page just now: {', '.join(price_hints)}"
        lines.append(line)
    if any_full_text:
        lines.append(
            "Some of the above are full page content (not just a search snippet), fetched "
            "and rendered live just now — treat that as substantially more reliable than a "
            "snippet, and prefer quoting/citing specifics from it over guessing."
        )
    lines.append(
        "Tell the user explicitly that this answer is based on a live online web search "
        "performed just now (e.g. \"Based on a live web search:\"), so they know it's "
        "fresh browsed data rather than your training knowledge."
    )
    lines.append(
        "Act as a research assistant: synthesize the sources instead of summarizing each "
        "one independently; corroborate important claims across multiple sources when "
        "possible; distinguish reported facts from inference; mention meaningful source "
        "disagreement or missing evidence; and attach the relevant source URL directly to "
        "each major factual claim. Never cite a source that does not support that claim."
    )
    if any_price_hints:
        lines.append(
            "You have live pricing data extracted directly from the retailer pages above, "
            "moments ago. Do not say you cannot browse the internet or lack real-time "
            "access — you do, via the data above. Answer using it directly: if multiple "
            "prices were found, present them as a comparison (a table works well), name "
            "the cheapest option, and cite the source link for each. If a specific source "
            "had no price hint, say so for that one source rather than declining to answer "
            "at all, and note that prices can change so the user should confirm at the link."
        )
    return "\n".join(lines)


# --- "should we even search?" gate, replacing the old manual composer toggle ---
#
# The specific-topic detectors above (weather/time/currency/news) are already precise
# enough to trust outright. Shopping is deliberately NOT included wholesale here even
# though looks_like_shopping_query() exists above - that function's own keyword list
# includes very common, topic-neutral words ("compare", "book", "available") that are
# fine as a secondary enrichment gate once a search is already happening, but far too
# broad to decide whether to search AT ALL ("compare bubble sort and quicksort" is not
# a shopping query). A narrower, purchase-specific phrase list is used instead.
_EXPLICIT_SEARCH_KEYWORDS = (
    "search for", "search the web", "search online", "look up", "look this up",
    "look online", "google ", "find out", "check online", "browse the web",
    "on the internet", "what does the internet say",
    # Added 2026-07-23 (follow-up to the same broadening request): "research" and a bare
    # "online" are both unambiguous asks for a live lookup ("research the best NAS
    # drives", "find reviews online"). "online" is a plain substring like every entry
    # here, so it also catches "check online prices", "available online", etc.
    "research", "online",
)
_RECENCY_KEYWORDS = (
    "today", "right now", "currently", "current price", "current status", "current version",
    "latest version", "latest release", "latest news", "this week", "this month", "this year",
    "as of now", "up to date", "up-to-date", "breaking news", "just announced", "just released",
    "happening now", "live score",
    # Broadened 2026-07-23 (user asked for search to fire far more readily): fresh-fact
    # phrasings that previously fell through and got a stale-knowledge answer or an "I
    # can't check" refusal. Still keyword-gated - timeless conceptual questions ("what is
    # recursion") deliberately stay off; see should_search_web()'s docstring.
    "latest", "newest", "most recent", "recently", "release date", "when did", "when does",
    "when will", "who won", "who is winning", "what happened", "is it out", "has it been released",
    "still available", "any news", "any updates", "update on", "status of", "did they",
    "have they", "announced",
    # Second broadening pass, same day (user: "add more aggressive triggers"): review/
    # comparison/spec/recommendation phrasings — questions whose answers are almost
    # always fresher online than in training data (product specs, benchmarks, reviews,
    # rankings, real-world opinions), even without an explicit recency word. Bare "vs"
    # is space-padded so it can't fire inside a word ("canvas", "conversation").
    "review of", "reviews for", "reviews of", "benchmark", "spec of", "specs of", "specs for",
    "specifications", " vs ", "versus", "compared to", "comparison of", "which is better",
    "which is best", "best way to buy", "top 10", "top ten", "top rated", "recommend",
    "recommendation", "worth it", "worth buying", "should i buy", "alternatives to",
    "alternative to", "is there a", "does anyone", "reddit", "opinions on", "thoughts on",
    "who is", "who was", "who are", "what version", "which version", "how popular",
    "market share", "how many people", "population of", "net worth", "stock price",
    "how old is", "when was", "where is", "where can i",
)
# A recent-enough year mentioned anywhere ("best laptops 2026", "GLM 5 benchmarks 2025")
# is a fresh-fact signal no keyword list can enumerate. Cutoff is intentionally rolling
# (current year minus 1, computed at import) rather than hardcoded so this never goes
# stale the way a fixed "2024" literal would have by 2027.
_RECENT_YEAR_RE = None  # set right below; kept module-level for testability


def _build_recent_year_re():
    import datetime as _dt

    this_year = _dt.date.today().year
    years = "|".join(str(y) for y in range(this_year - 1, this_year + 2))
    return re.compile(rf"\b(?:{years})\b")


_RECENT_YEAR_RE = _build_recent_year_re()
_SHOPPING_SEARCH_TRIGGER_KEYWORDS = (
    "price of", "prices for", "cost of", "how much does", "how much is",
    "cheapest", "best deal on", "discount on", "where to buy",
)
# "browse" (in any form) is about as unambiguous a web-access request as plain English
# gets - unlike _EXPLICIT_SEARCH_KEYWORDS above, which needs a specific phrase match,
# a bare "browse" anywhere in the message is trusted on its own. Deliberately separate
# from should_search_web()'s other signals: _web_search_context() checks this
# specifically to scrape more results, more thoroughly, than the default search does -
# see its own docstring for why (asked explicitly to "browse", not just answered from
# background search).
_BROWSE_KEYWORDS = ("browse", "headless browser", "headless chrome", "use playwright", "chrome browser")


# Screenshots (and per-page content images) in the chat reply are opt-in per message
# (2026-07-24, user request): initially every browsed page attached them
# unconditionally, which cluttered ordinary factual answers with 4+ images and added
# capture/download latency to every auto-triggered search. Now they only attach when
# the message actually asks for visuals. Substring-matched like every other keyword
# list here — "screenshot" also catches "screenshots", "show me the page" catches
# "...pages", etc.
_SCREENSHOT_KEYWORDS = (
    "screenshot", "screen shot", "screen grab", "screengrab", "screen capture",
    "capture the page", "show me the page", "show the page", "picture of the page",
    "image of the page", "what does the page look like", "what does it look like",
)


def wants_screenshots(query: str | None) -> bool:
    if not query:
        return False
    q = query.lower()
    return any(kw in q for kw in _SCREENSHOT_KEYWORDS)


def looks_like_browse_request(query: str | None) -> bool:
    if not query:
        return False
    if extract_urls(query):
        # A raw URL is as unambiguous a browse ask as it gets, keyword or not - see
        # extract_urls' docstring for the real report this fixes (a bare URL with no
        # other words matched none of the keyword lists below).
        return True
    q = query.lower()
    return any(kw in q for kw in _BROWSE_KEYWORDS)


def is_explicit_search_request(query: str | None) -> bool:
    """True only when the user directly asked to search/browse or supplied a URL."""
    if not query:
        return False
    q = query.lower()
    return (
        bool(extract_urls(query))
        or looks_like_browse_request(query)
        or any(keyword in q for keyword in _EXPLICIT_SEARCH_KEYWORDS)
    )


def contextualize_search_query(query: str, messages: list[dict] | None) -> str:
    """Resolve vague follow-ups such as "online search" from recent chat context.

    Search commands contain almost no topical terms themselves. Searching the literal
    command yields irrelevant results (correctly rejected by the relevance filter), so
    carry forward the preceding user subject and a short assistant excerpt that may hold
    a location/entity established earlier in the conversation.
    """
    if _search_terms(query):
        return query
    prior = list(messages or [])
    # Drop the current user message when it is present in the supplied chat history.
    if prior and prior[-1].get("role") == "user" and prior[-1].get("content") == query:
        prior.pop()
    previous_user = next(
        (str(m.get("content") or "").strip() for m in reversed(prior)
         if m.get("role") == "user" and str(m.get("content") or "").strip()),
        "",
    )
    previous_assistant = next(
        (str(m.get("content") or "").strip() for m in reversed(prior)
         if m.get("role") == "assistant" and str(m.get("content") or "").strip()),
        "",
    )
    context = " ".join(part for part in (previous_user, previous_assistant[:500]) if part)
    return f"{context} {query}".strip() if context else query


def should_search_web(query: str | None) -> bool:
    """Decides whether a chat message needs a live web lookup at all. Replaces the old
    manual per-message toggle: the specific-topic detectors below are strong signals on
    their own; everything else needs an explicit search-style phrasing, a recency-flavored
    phrase, or a recent year mentioned before this pays the latency/noise cost of a
    search. Deliberately broadened 2026-07-23 (user request: search should fire far more
    readily, erring toward searching) — but a plain timeless conceptual question ("what
    is recursion", "explain the French Revolution") still stays off, relying on the
    model's own knowledge, since a live search adds nothing there but latency."""
    if not query:
        return False
    if (
        looks_like_weather_query(query)
        or looks_like_time_query(query)
        or looks_like_currency_query(query)
        or looks_like_news_query(query)
        or looks_like_browse_request(query)
    ):
        return True
    lowered = query.lower()
    if any(
        kw in lowered
        for kw in _EXPLICIT_SEARCH_KEYWORDS + _RECENCY_KEYWORDS + _SHOPPING_SEARCH_TRIGGER_KEYWORDS
    ):
        return True
    return bool(_RECENT_YEAR_RE.search(query))
