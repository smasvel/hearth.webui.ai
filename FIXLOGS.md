# Hearth fix logs and development history

Historical repair investigations, implementation notes, and validation records moved
from the main README. Earlier entries may be superseded by later fixes. For current
features, setup, and usage, see [README.md](README.md).

### Admin-chat host security scanner

Admins can run a defensive host audit directly in chat by entering:

```text
/security-scan
```

The admin header also has a **Security** button that submits the same exact command. A model does
not need to be loaded. The resulting Markdown report is streamed into the conversation and saved
in chat history like a normal assistant response.

This is an application-owned diagnostic, not shell access for the model:

- Only the two exact spellings `/security-scan` and `/security scan` trigger it. Natural-language
  chat is never treated as a command.
- The route checks the authenticated server-side session role. Non-admin users receive `403`;
  hiding the button is only a UI convenience, not the security boundary.
- `security_scan.py` contains a fixed allowlist of read-only commands. It accepts no command or
  arguments from chat, never uses `shell=True`, applies short timeouts, and caps captured output.
- The web service never asks for or stores a sudo password. It may run one exact privileged
  command, `/usr/sbin/lynis audit system --quick --no-colors`, through the narrow sudoers rule
  described below. No other passwordless sudo command is granted.
- Checks currently cover pending APT/security updates, non-loopback TCP listeners and known
  high-risk ports, failed services, automatic-update status, sensitive-file permissions,
  world-writable application files, and availability of deeper audit tools.

Install Lynis and the included exact-command sudoers rule once:

```bash
sudo apt install lynis
sudo install -o root -g root -m 0440 deploy/hearth-lynis-sudoers /etc/sudoers.d/hearth-lynis
sudo visudo -cf /etc/sudoers.d/hearth-lynis
```

The two `sudo` setup commands above prompt normally. Afterward, the chat scan can run only the
fixed read-only Lynis audit without a password; `sudo -n true` and every unrelated sudo operation
remain denied. Lynis has a five-minute timeout and its chat result is summarized; its full report
remains in `/var/log/lynis.log`.

The scanner is triage, not a proof that a host is impossible to compromise. Keep system security
updates current separately:

```bash
sudo apt update
sudo apt full-upgrade
```

Keep application credentials and the TLS private key readable only by their owner.
Recommended permissions for every deployment:

```bash
chmod 600 .env .env.postgres certs/key.pem
chmod 644 certs/cert.pem
```

The same audit deliberately reports network exposure instead of changing it automatically.
In particular, a VNC listener on `0.0.0.0:5902`/`[::]:5902` is high risk and should be restricted
to a trusted VPN/firewall or rebound to loopback and reached through an authenticated tunnel.
Changing that remotely can disconnect the current administrator, so it is never an automatic
chat action.

**Deleting a model** (`/api/models/delete`, confirmation dialog client-side before it ever fires)
frees disk space, and unloads first if it was loaded — same "shouldn't leave a dangling loaded
thing behind" reasoning as deleting a user cleaning up their conversations. This is a real
asymmetry between the two backends, not a bug: **Lemonade models actually get deleted** —
`POST /v1/delete` is a real, documented, working endpoint. **LM Studio models refuse to delete** —
`lms` has no delete/rm/uninstall command at all, and its own HTTP API's reported model `path` was
confirmed live to not reliably match real files on disk (`qwen/qwen3.6-35b-a3b` reported path
`"qwen/qwen3.6-35b-a3b"`, which doesn't exist — the real download sits at
`~/.lmstudio/models/lmstudio-community/Qwen3.6-35B-A3B-GGUF/`, a completely different location).
Catalog model ids are logical identities LM Studio doesn't expose a reliable mapping from to
physical files, so `lmstudio_client.delete_model()` deliberately raises an explanatory error
instead of guessing at `rm -rf` targets for multi-GB files with no undo — delete those from the LM
Studio app's own **My Models** tab instead, which has the authoritative mapping this app doesn't.

Chat history is **private per account** — each user only ever sees their own conversations,
enforced at the database query level (every conversation/message lookup is scoped by the logged-in
user's ID), not just filtered in the UI. Deleting a user also deletes their conversations.

### Users page

User management moved from a header flyout to its own page (`GET /users`, `templates/users.html` +
`static/users.js`, admin-only via the same `admin_required` decorator as the API routes — a
non-admin hitting the URL directly gets a plain 403, same as before). Three real asks bundled into
one page:

- **Add multiple users** — same form as before (username/password/role), just relocated.
- **Proper pagination** — `users_store.list_users()` now takes `page`/`per_page` and returns
  `{users, total, page, per_page, total_pages}` (`LIMIT`/`OFFSET` under the hood, capped at 100 per
  page), with Prev/Next controls on the page. Verified live by creating 22 throwaway accounts:
  correctly split 20/4 across two pages, then all cleaned up.
- **Password reset, not retrieval** — asked for as "enough encryption to retrieve or store the
  password"; worth being direct about what shipped instead and why. Passwords were already hashed
  properly (Werkzeug's salted `generate_password_hash`, not reversible by design — true of any
  correctly-built password store, not specific to this app). Building actual retrieval would have
  meant switching to reversible encryption, a real security downgrade: anyone who ever got the
  database would get everyone's actual password, not just unusable hashes. Confirmed this wasn't
  the intent before building anything. What exists instead is `users_store.set_password()` +
  `POST /api/users/<id>/reset-password` — an admin sets a *new* password for someone (e.g. after
  they forget theirs), which achieves the same real-world goal without ever making a password
  recoverable. `window.prompt()` for the new password, same low-ceremony pattern as
  `window.confirm()` already used for deletions elsewhere in this app. Verified live: reset a test
  account's password, logged in with the new one successfully, confirmed a too-short password is
  still rejected the same as account creation.

### Follow-up: a dedicated personal Caddy LAN token per user

Asked for directly: "make sure when user is added, make sure to give a caddy dedicated token."
Until this, every LAN client shared one of three fixed tokens (see "Bigger context ceiling +
per-client traffic attribution" below) — fine for telling *opencode* from *vscode* apart, but not
for telling people apart, or revoking one person's LAN access without breaking everyone else's.

`users_store.py` gained a `caddy_token` column (`secrets.token_hex(32)`, generated once at account
creation, migrated in for existing rows on `init_db()`), plus `get_caddy_token(user_id)` and
`regenerate_caddy_token(user_id)`. This app still **cannot write to Caddy's own config file**
(same constraint noted throughout this doc — it's root-owned and this app deliberately never
touches it), so a token existing in the database doesn't make it live on its own. What ships
instead: `POST /api/users` now returns a ready-to-paste Caddyfile snippet alongside the created
user (`_caddy_matcher_name()` / `_caddy_user_snippet()` in `app.py`) — a `@user_<name>_<id6>`
bearer-token matcher plus one `reverse_proxy` line per backend port — with the exact
`caddy validate && systemctl reload caddy` command to apply it. The Users page shows this in a
copyable card the moment a user is created, and a "LAN token" / "Regenerate token" button pair on
every user card (`GET /api/users/<id>/caddy-token`, `POST /api/users/<id>/regenerate-caddy-token`)
brings the same snippet back later — regenerating warns explicitly that anything already using the
old token (e.g. a device someone set up earlier) will need updating once the new snippet is pasted
in.

Scoped down from two AskUserQuestion rounds: automation stayed at "app generates the token, an
admin pastes it in" rather than attempting to write the Caddyfile automatically (the read-only
relationship with Caddy's config is a deliberate, existing boundary, not an oversight to fix here),
and the token's purpose was confirmed as personal LAN API access (same OpenAI-compatible endpoints
Continue/OpenCode use), not a general account credential.

### NPU utilization / TOPS in the message meta line

Ryzen AI (Lemonade) responses show `~<used>/<max> TOPS (NPU <percent>%)` next to the token count in
each message's meta line — asked for explicitly, and worth being upfront about what it actually is:
**neither backend reports real per-response compute**, unlike tokens, which both report precisely.
What's shown is a live instantaneous reading of `GET /v1/system-stats`' `npu_percent`, sampled right
as generation finishes, multiplied against the NPU's rated max (`GET /v1/system-info`'s
`tops_max_int`, 58 on this XDNA2 chip — cached in `lemonade_client.py` since it's static hardware
info, not re-fetched every message). It's a snapshot at that moment, not an average over the whole
response. LM Studio responses never show this at all (no NPU involved, backend-gated in `app.py`
rather than showing a misleading zero on every message).

**Real limitation found while verifying this, not a bug in this app**: on this Lemonade version
(11.0.0), `npu_percent` reports `null` and `system-info`'s NPU `utilization` field reports a flat
`0.0` *consistently, including while actively generating on the NPU* — confirmed by polling both
endpoints every second during a real generation and watching `gpu_percent` correctly climb to 44%
while the NPU fields never moved. So today, on this machine, the TOPS display will usually just be
absent (same fail-soft behavior as everywhere else here) even on genuine Ryzen AI responses — not
because anything here is broken, but because Lemonade's own NPU telemetry doesn't work yet. Worth
re-checking after a Lemonade update; nothing on this app's side needs to change if/when it's fixed,
since `lemonade.npu_utilization_percent()` just passes through whatever the server reports.

### Settings panel

Admin-only (top header, next to Models/Users). Lets an admin change where this app reaches LM
Studio and Lemonade (`base_url`) and what bearer token it sends each (`api_key`), from the UI
instead of only `.env` + a service restart. Backed by the `settings.key/value` PostgreSQL table
(`settings_store.py`, same DB as everything else) — `lmstudio_client.py`/`lemonade_client.py` no
longer capture `LMS_BASE_URL`/`LMS_API_KEY` as frozen module constants at import time; `base_url()`
and `api_key()` are now functions that check the settings table fresh on every single call
(falling back to the `.env` value, then a hardcoded default), so a change saved in the panel takes
effect on the *next* request, no restart needed — verified live: set a test Lemonade API key
through `/api/settings`, confirmed it was immediately in effect in the already-running process,
persisted to the DB, then cleared it again.

**Revised after initial build**: API keys were first shown as only a `*_api_key_set: bool` (never
the real value), on the theory that the upstream LM Studio/Lemonade credentials deserved stricter
treatment than the Caddy bearer token (which every logged-in user already sees in plaintext in the
Help panel below). Changed on request to show the actual current value in the field, plain text —
the panel is already admin-only, so the extra masking was friction without a real security boundary
behind it for the one role that can already see/change everything else here. `POST /api/settings`
semantics simplified alongside this: since the real value is always visible, an empty field
unambiguously means "clear it" now (matching how `base_url` already worked), so the earlier
separate "Clear API key" checkbox was removed as redundant.

**Caddy info is now shown here too**, read-only: the shared bearer token and both LAN ports
(`:8443` LM Studio, `:8444` Lemonade), parsed live from `/etc/caddy/Caddyfile` on every
`GET /api/settings` call (`_read_caddy_info()` in `app.py`) rather than hardcoded a second time the
way the Help panel's copy is — confirmed the file is world-readable (`0644 root:root`) so no sudo
is needed to read it as this app's own unprivileged user. This app still never writes to the
Caddyfile itself (same reason the `:8444` Colibri-block cleanup earlier had to be handed to the
user to run) — it's informational display only, matching what the Help panel already shows LAN
clients, just in one more place for convenience.

**Test buttons** (`POST /api/settings/test`, four `target`s: `lmstudio`, `lemonade`,
`caddy_lmstudio`, `caddy_lemonade`) let you verify a config actually works before relying on it.
The two backend tests deliberately test **whatever's currently typed in the form**, not what's
already saved — sent straight through to a one-off `GET .../api/v0/models` (LM Studio) or
`GET .../v1/health` (Lemonade) request, bypassing `settings_store` entirely, so you can check a
change before committing it with Save (a blank field in the test request falls back to the saved
value, same as leaving it blank always has). The two Caddy tests hit `https://localhost:<port>/...`
through the actual reverse proxy with the real bearer token from the parsed Caddyfile — useful after
hand-editing that file, which has happened more than once this session (the `:8444` Colibri cleanup,
adding the Lemonade block). All four verified live: real success responses (model counts, loaded
model name, authenticated `200`s) and a real failure case (wrong port → connection-refused message
surfaced verbatim, not swallowed).

A **Help** panel (top header, visible to every logged-in user — unlike Models/Users, which stay
admin-only) has the config + CA cert needed to point [Continue](https://continue.dev) (VS Code) or
[OpenCode](https://opencode.ai) on another machine at this LM Studio instance over the LAN, via the
Caddy reverse proxy on `:8443` (`/etc/caddy/Caddyfile` — enforces a real bearer token, unlike LM
Studio's own unenforced API key setting). The CA-cert-and-OS-trust steps are shared by both tools;
each then gets its own config block. Static reference content with a copy button for each block;
update it by hand in `templates/index.html` if the Caddy setup, model, or token ever changes. Note
this does mean every account can see that bearer token, not just admins.

Getting Continue's *chat* actually working over this took real debugging (worth knowing if this
setup changes and it breaks again): a plain `curl --cacert <the CA cert> ...` from the client
machine worked fine — proving network, TLS, and Caddy auth were never the problem — but Continue's
chat still failed with a generic `Connection error`. The stack trace pointed at the OpenAI Node
SDK's own `fetch()` (Continue's `useOpenAIAdapter: true` streaming path), which — at least on
macOS — doesn't reliably honor a config-level `caBundlePath` for a self-signed CA the way curl
does. The actual fix was installing the CA cert as a trusted root in the **OS's own certificate
store** (`security add-trusted-cert` on macOS; `certutil -addstore "ROOT"` on Windows), not
anything in `config.yaml`. The Help panel documents both, as a shared prerequisite before either
tool's own config — OpenCode is also a Bun-based CLI, so the same fetch/CA-trust quirk is worth
checking first there too if it fails to connect.

### Follow-up: one-line CA trust instead of manual cert install

Asked for as "enable automatic https for caddy instead of manually adding certs" — worth being
direct about what's actually possible here before what shipped. A public CA (Let's Encrypt,
ZeroSSL) categorically **cannot** issue a certificate for a private IP or LAN-only hostname like
this machine's; domain validation requires public DNS/HTTP reachability, a hard protocol
constraint, not a config gap. Caddy's `tls internal` (already in use here) *is* Caddy's own
automatic-HTTPS answer for exactly this case — certs are auto-issued and auto-renewed from Caddy's
local CA with zero server-side cert management already. Confirmed via AskUserQuestion that the
real goal was reducing the one genuinely manual step left: trusting that CA on each client.

New unauthenticated `GET /ca-cert` route in `app.py` serves the live CA cert directly from this
app (a public cert, not a secret — deliberately open so a client can fetch it before it has any
other reason to reach this machine yet). `help_data.ca_trust_commands(ip, app_port)` turns that
into one copy-paste command per OS — `curl` + `sudo update-ca-certificates` on Linux,
`curl` + `security add-trusted-cert` on macOS, `Invoke-WebRequest` + `Import-Certificate` on
Windows PowerShell — each saving the cert to the home directory (not `/tmp`, since tools like
Continue need a `caBundlePath` that still exists later, and `/tmp` can clear on reboot). The Help
panel's old two-step "download this file, then follow several OS-specific GUI steps" flow is now
one line per OS under a single "Step 1 — Trust the CA"; the raw PEM is still there for anyone who
wants it, just demoted to a collapsed fallback.

Verified end-to-end, not just by inspection: ran the real Linux one-liner against an isolated
`$HOME`, confirmed with `openssl verify` that the cert became genuinely trusted, then confirmed a
plain `curl` (no `-k`) succeeded with full certificate verification against a real Caddy LAN port
— then cleaned up the test artifacts and re-confirmed the pre-existing trust on this machine was
undisturbed.

## Cross-chat memory

Cross-chat recall embeds messages in PostgreSQL, scopes retrieval by user, and
excludes the current conversation. It uses Lemonade embeddings, deduplicates
near-identical chunks, and retrieves up to eight relevant chunks with a minimum
similarity of 0.35. Short conversational facts may score below repeated questions,
so both the similarity threshold and retrieval window matter. Deleting a
conversation or account also deletes its associated memory chunks.

## Web search ("online research")

Controlled globally from **Settings → Web search and research**. The master switch can disable
all searching immediately; activation can be **Automatic** (via
`web_search_client.should_search_web()`) or **Explicit only** (the user must directly ask to
search/browse or paste a URL). Admins can independently cap organic search results (1–10), cap
Playwright-opened pages (0–5), disable all browser page opening for snippet-only research, and
allow/deny direct opening of user-pasted URLs. These values persist in `settings_store` and take
effect without a restart. Relevance filtering, sponsored-result rejection, ad/tracker blocking,
and domain diversity are mandatory and cannot be disabled from the UI.

When triggered, `web_search_client.py` runs a live search via [`ddgs`](https://pypi.org/project/ddgs/)
(DuckDuckGo metasearch, no API key needed — picked deliberately over adding a paid search API key
to `.env`, consistent with this app having no other cloud dependencies) and folds the configured results
(title/URL/snippet) into the system message, same injection pattern as cross-chat memory above
(`_web_search_context()` sits right next to `_recall_memory()` in `app.py`, same fail-soft
contract: any failure just means no search context, never a broken chat).

**Price comparison / ticket availability.** A plain search snippet is usually just a
description ("Shop the Whirlpool WTW5057LW..."), not an actual number — which is why models
would say "I can't browse live inventory" even with search results injected: they genuinely
had no price in front of them, and years of training on "I don't have real-time access"
disclaimers makes that the reflexive answer whenever the context is thin. When the query looks
like a price/ticket/availability question (`looks_like_shopping_query()` — keyword check on
things like "price", "cheapest", "ticket", "in stock", "compare"), `enrich_with_price_hints()`
concurrently fetches the top 4 result pages directly and regex-extracts real price figures
from their raw HTML (schema.org/JSON-LD product markup, `itemprop="price"`, `og:price` meta
tags, or plain `$XX.XX` text as a last resort), then appends an explicit instruction to the
injected context telling the model it does have live data and shouldn't deflect. Verified live
against a real product query (Whirlpool WTW5057LW): pulled real current prices ($758, $932)
straight off a retailer's page. Best-effort only — many retail sites (Best Buy, Lowe's) render
price client-side via JS that a plain GET never sees, so some sources in a comparison will
legitimately come back with no price hint; the injected instruction tells the model to say so
for that source rather than declining to answer the whole question.

**News queries.** A plain text search on a news-flavored question ("what's today's news")
returns homepage links (cbsnews.com, nbcnews.com, ...) with generic site-description snippets —
nothing resembling an actual headline, which is why this used to just fail to produce a useful
answer at all. `looks_like_news_query()` routes these to `search_news()`, which uses DDGS's
dedicated news search (`DDGS().news()`, 1-day recency filter) instead of the general text search,
returning real dated articles. For a *generic* "what's the news" ask with no specific subject
(`_is_generic_news_request()` — strips question filler and checks whether any topical word
survives), searching the literal question text still performs badly (confirmed live: 1 stale,
irrelevant result, since ddgs treats it as literal keywords, not "give me a digest"); instead
`search_news()` queries a handful of broad topic seeds (world/business/technology/US news) and
merges the results, deduplicated by URL, into a front-page-style digest — much closer to what
glancing at an actual news homepage gives you. A topic-specific query ("news about the stock
market") searches the literal text directly, since it's actually meaningful there.
`format_news_for_context()` includes each article's date (the whole point of a news query is
"is this current"), and hedges the same way price comparison does — telling the model not to
deflect with "I can't browse the internet" when it's holding live, dated results.

**Weather queries.** Same class of failure as prices and news: search snippets for "weather in
New York today" are just site descriptions ("AccuWeather provides hour-by-hour temperatures...")
with no actual temperature in them. `looks_like_weather_query()` routes these to
`fetch_weather()`, which resolves the place name via Open-Meteo's free geocoding API and pulls
real current conditions + a 3-day forecast from their forecast API (no API key — same
no-cloud-keys rule as ddgs). Location extraction (`extract_weather_location()`) prefers an
"in <place>" tail, falls back to whatever non-filler words survive ("Tokyo weather" → "Tokyo"),
and if nothing topical survives (a bare "what's the weather") falls through to the general
search path rather than guessing a city. Verified live end-to-end: "What is the weather today in
New York" now answers with the actual current temperature, conditions, humidity, wind, and
forecast — previously it had nothing but site names to work with.

**World time & currency conversion.** Two more members of the same "answer lives behind the
link" class, found by re-auditing the breadth-check categories end-to-end rather than trusting
the snippet-count pass: world-clock sites all render the actual time via JS (confirmed live —
every snippet was a site description, zero contained a time), and currency-converter sites
frequently omit the actual rate from their crawlable text (confirmed live — one full result set
had no rate figure at all). Time queries (`looks_like_time_query()`) need no time API at all:
Open-Meteo's geocoder (already used for weather) returns each place's IANA timezone id, and
Python's `zoneinfo` computes the current time locally. Currency queries
(`looks_like_currency_query()` — requires both a recognizable currency *pair*, by ISO code or
spoken name like "dollars to rupees", and a rate/convert-flavored keyword, so "the dollar is
strong" prose doesn't trigger it) go to Frankfurter's free ECB reference-rate API (no key, same
rule as ddgs/Open-Meteo); the injected context states the as-of date and that ECB rates are
daily, not tick-level, and tells the model to mention consumer rates differ. Amounts are
honored ("convert 100 dollars to rupees" → 100 USD = 9657 INR, with the unit rate alongside).
All special routes fail soft to the general search path, which was verified to still return
usable (if weaker) results when a data API is transiently down.

**Breadth check.** Tested the research path across 14 varied question types through
the real `_web_search_context()` code path: tech, news, biography, science, math, astrology,
astronomy, origins-of-life, history, health, weather, stocks, sports, currency conversion. All
return usable, snippet-bearing results; stocks additionally benefit from the price-hint
enrichment (the "price" keyword triggers it, usefully), and sports/history/science answers were
present directly in the snippets. Weather was the one genuine failure found, fixed above.
Six of those categories (biography, science, math, astrology, astronomy, origins-of-life) were
additionally verified end-to-end — injected context fed through a real chat completion against
the live NPU model — and every one produced a substantive, factually-correct answer: Karikó's
2023 Nobel (shared with Weissman), JWST-vs-Hubble optics/infrared reasoning, Collatz correctly
reported as still unsolved, a current same-day Scorpio horoscope (with the model adding its own
entertainment-only hedge, unprompted), a recent real exoplanet discovery (MIT's disintegrating
planet with a comet-like tail) pulled from the live results rather than training data, and the
~3.48-billion-year-old Australian stromatolites for earliest fossil life.

Deliberately **not** implemented as real LLM-decided tool-calling (the model emitting a
`tool_calls` response that this app then executes) — debugging the OpenCode integration earlier
already surfaced how unreliable local models can be at clean structured tool-call output (a
missing-field schema error that turned out to be a genuine model-capability gap, not a config
issue). Pre-fetching results and injecting them as plain context, the same way memory recall
already works, sidesteps that failure mode entirely —
verified live: asked a reasoning model about *today's* AMD NPU Linux driver news with the toggle
on, and its reasoning trace explicitly quoted real, current Phoronix article titles and URLs that
only exist in the injected search results, not training data.

### Automatic detection replaced the manual toggles

Both the 🌐 web-search toggle and the standalone **Image** composer button are gone now — once
image-request detection (`comfyui_client.looks_like_image_request()`, above) existed and proved
reliable, the user pointed out the natural next step: if the app can already tell whether a message
needs to go online or generate an image, making the user flip a switch or press a separate button
first is just friction the detection was supposed to remove. `should_search_web()` in
`web_search_client.py` is the new gate, and it's checked unconditionally on every message now
instead of behind `body.get("web_search")`.

**Deliberately biased toward *not* searching, unlike image detection.** Missing an image request
just means a text reply instead of a picture — annoying but cheap to notice and correct ("draw
me..." always works). Searching when it wasn't warranted is a worse failure mode by default: every
search adds a few seconds of latency and can inject an unrelated result set into a question the
model already knew perfectly well, which is *harder* to notice than a missing image ("why is this
answer about bubble sort citing a shopping site?"). So `should_search_web()` only fires on: the
existing specific-topic detectors (weather/time/currency/news — already precise), explicit
search-style phrasing ("search for...", "look up...", "on the internet"), an explicit recency word
("today", "latest", "currently", "this week", ...), or a narrow purchase-specific phrase ("price
of...", "where to buy..."). Plain factual/conceptual/coding/creative messages stay off, same as
before with the toggle simply left unflipped.

**`looks_like_shopping_query()` was deliberately *not* reused for this gate**, even though it
already existed and looks like the obvious fit. That function's keyword list includes "compare",
"book", and "available" — fine as a secondary decision *once a search is already happening*
(whether to also spend a few extra seconds extracting price hints from the result pages), but far
too broad to decide whether to search at all: "compare bubble sort and quicksort" would have
triggered a real web search for a pure algorithms question. Caught before shipping by testing
`should_search_web()` against 18 hand-picked cases (weather/news/currency/time/explicit-search/
recency → true; coding/conceptual/creative/small-talk, including that exact "compare" example →
false) — all 18 passed. `looks_like_shopping_query()` itself is untouched and still does its
original, narrower job inside `_web_search_context()`.

The `/api/images/generate` HTTP endpoint and `_web_search_context()`/`should_search_web()` plumbing
are otherwise unchanged — only the two manual UI triggers (button + toggle, in `templates/index.html`
and their wiring in `static/app.js`) were removed. The composer is now just a text box and Send —
typing "what's the weather" or "draw me a cat" and hitting Send is the entire interface for both
features.

### Real page scraping via headless Chromium, for research-heavy queries

DDGS search snippets are short (~300 characters) and are frequently just a homepage-style blurb,
not the actual answer — the exact failure pattern behind nearly every fix in this section so far
(weather, world time, currency, news all needed a dedicated real data source instead of a snippet).
General research queries ("look up how X works", "what's the deal with Y") don't have a dedicated
API the way weather/time/currency do, so the fix here is more general: render the actual result
pages in a real headless browser ([Playwright](https://playwright.dev/python/) + Chromium) and hand
the model real article text instead of a snippet.

**`page_scraper.py`** is a new, small module: `fetch_pages(urls)` launches a short-lived headless
Chromium (`playwright install chromium`, ~180MB browser binary, separate from the pip package —
see Setup above), visits each URL, strips `script/style/nav/header/footer/aside` before reading
text, and returns both the clean visible text *and* the fully-JS-rendered HTML per URL — one page
load serves both a readable-text need and a regex-scanning-rendered-markup need (prices), so
nothing has to visit the same URL twice. One browser launch per call rather than a persistent
shared instance: Playwright's sync API isn't safe to use across threads, and this Flask app runs
threaded (`threaded=True`, one OS thread per request) — launch overhead measured live at ~0.3-0.5s,
small enough that a fresh browser per search is simpler and safer than managing a persistent
instance with cross-thread synchronization. Chromium is launched with `--disable-gpu`: this
machine's iGPU is shared with the LLM backends and had a real GPU-memory crash earlier the same
day (see the MTP-vs-NPU benchmark section) — scraping plain page text has no need for
GPU-accelerated compositing, so there's no reason to let Chromium's default GPU usage risk
contending with whatever model is currently loaded.

**`web_search_client.enrich_with_full_text()`** (new) and **`enrich_with_price_hints()`** (rewritten)
both call into `page_scraper` now. Non-shopping general queries get `enrich_with_full_text` — the
top 3 results are rendered and their real article text (up to 2500 chars) replaces the DDGS
snippet in the injected context. Shopping queries still get `enrich_with_price_hints`, now
rendering pages in the real browser first (closing the exact gap its own old docstring flagged:
"many retail sites render price client-side via JS that a plain GET never sees") and falling back
to the previous plain-`requests.get()` approach only if Playwright isn't installed at all
(`page_scraper.available()`), so a deployment that skipped `playwright install chromium` still
gets *a* result, just the older, thinner one. `format_for_context()` prefers `full_text` over
`body` when present and raises the per-result cap from 300 to 1500 characters.

**Debugging case study: the original plan was "scrape DuckDuckGo directly with Playwright" — verified
live that this doesn't work.** The initial ask was to use Playwright against DuckDuckGo's own HTML
endpoint (`html.duckduckgo.com/html/`) for search, not just for visiting results. Tested directly:
the very first request already got DDG's JS anomaly-challenge page instead of results; a second
attempt with a realistic desktop Chrome user-agent got a hard "if this persists, please email us"
error instead. This is DDG's own anti-bot detection, and it triggered within two requests even with
basic fingerprint countermeasures — confirming (rather than assuming) that the `ddgs` package this
app already depends on for search discovery is doing real, necessary work to stay unblocked that a
naive Playwright script doesn't replicate. Building a parallel, less-robust DDG scraper would have
been a reliability *regression*, not an upgrade. The architecture that shipped instead keeps `ddgs`
exactly where it already worked (finding results) and adds Playwright only for the other half of the
original ask — visiting the ordinary websites those results point to, which aren't running anti-bot
detection against a research tool.

**Verified end-to-end, through a real chat completion, not just the injected context**: asked
`qwen3.6-moe-35b-a3b` "look up how the AMDXDNA accelerator driver's new power modes work in Linux
7.2" — the model correctly cited specific technical details (clock gating, the "low"/"medium" power
mode split, the `drm-misc-next` pull request) that only exist in the full scraped Phoronix article
text, not in a snippet, and correctly attributed the source. Also verified the price-hint path
still works after the rewrite: a real product query pulled `$757.70` / `$932.00` / `$174.30`
directly off a retailer's rendered page, same as the pre-Playwright version used to for pages
without JS-rendered pricing, now working for JS-rendered ones too.

### Explicit "browse and search" phrasing — re-verified DDG blocking, then built the achievable version

Asked again, later the same day, to specifically route "browse and search" style requests to a
Playwright script querying `duckduckgo.com` directly through headless Chrome. Given the case study
just above already found this doesn't work, it was re-tested rather than assumed still-broken or
assumed newly-fixed — via a *different* method this time, in case the first test had only caught
one specific weak point: navigated to the real `duckduckgo.com` (not the `/html/` lite endpoint)
with a realistic Chrome fingerprint, then interacted with the page like an actual user would (typed
into the search box, pressed Enter) rather than just loading a URL with a query parameter. Result:
DDG redirected straight to `static-pages/418.html` — literally HTTP 418 "I'm a teapot," DDG's own
explicit bot-block response. Two different techniques, two different failure signatures
(JS-challenge-then-email-error the first time, a direct 418 block this time), same underlying
conclusion confirmed twice: a Playwright session querying DuckDuckGo itself gets detected and
blocked reliably, not intermittently. Not built — a feature that reliably fails in production is
worse than no feature, and would look deceptively like it worked in a first demo.

**What shipped instead**: `web_search_client.looks_like_browse_request()` — a new, deliberately
low-bar detector (a bare "browse" anywhere in the message is trusted on its own, unlike
`should_search_web()`'s other signals which need a specific phrase or keyword) that recognizes an
explicit ask to browse. When it fires, `_web_search_context()` in `app.py` doesn't route through
DuckDuckGo-via-Playwright (still doesn't work, per above) — it routes through the same
Playwright-page-scraping machinery already built and working (`enrich_with_full_text`), just with
`max_fetch=5` instead of the usual 3, and an explicit note appended telling the model this was
gathered "by actually browsing: DuckDuckGo search plus a real headless Chrome browser (Playwright)
rendering and reading each page just now" — so it answers with the confidence the user's explicit
"browse" ask implies, rather than hedging. This is an honest description, not a stretch: headless
Chrome genuinely is doing the browsing here, just of the ordinary result pages rather than of DDG's
own search results page (which was never going to work, confirmed twice above).

Verified end-to-end through a real chat completion: "browse and search for the latest AMD Ryzen AI
NPU driver updates" correctly triggered the 5-result scrape and produced a detailed, specific,
version-numbered answer (driver version, supported chip architectures, bundled Adrenalin Edition
release) pulled from AMD's own documentation and driver release pages — not a hedge about lacking
browser access.

### Follow-up: "browse all the deals for washing machine... still not using playwright"

A real query matched *both* `looks_like_shopping_query()` ("deals") and
`looks_like_browse_request()` ("browse"), and the shopping branch is checked first — so it went
through `enrich_with_price_hints()`, not the dedicated browse branch above. Investigated whether
that was actually the bug (i.e. whether Playwright genuinely didn't run): it wasn't — confirmed
live that `enrich_with_price_hints()` was already rewritten earlier to use `page_scraper`/Playwright
too (see "Real page scraping via headless Chromium" above), and running the exact query through
`_web_search_context()` directly returned real scraped page content (thespruce.com, amazon.in), not
snippets. Playwright *was* running. The real gap was narrower: that branch never had the explicit
"you did browse this" directive the dedicated branch does, so the model had no particular reason
not to hedge — and separately, the actual search results for this specific query were weak (generic
care/buying-guide articles, not real retailer deal listings), which is a DDGS result-quality
limitation, not a Playwright one.

Fix: extracted the directive into a shared `_browse_framing()` helper and applied it to *both*
branches whenever `looks_like_browse_request()` matches, not just the dedicated branch — the
shopping branch is deliberately still checked first even when both match (it returns strictly more
data, both `price_hints` *and* `full_text`, than the browse-only branch), it just needed the same
framing wrapped around its result. Also bumped its `max_fetch` from 4 to 5 in this case, matching
the dedicated branch's depth. Verified live: the exact reported query now returns a note starting
with the same "You DID just browse the web for this..." directive verified working in the section
above.

### Debugging case study: still refusing despite correct data — a weak-model instruction-following gap, not a plumbing bug

Reported as "still not able to browse through playwright," with a real pasted example: asked
`"Can you search about the pregnancy pain through the browser"`, got back *"I cannot browse the
internet or access live web content..."* Traced via `chat_history.db` (same technique as the
earlier image-hallucination and radar case studies) to the exact model used: **`QwenFLM17B`**
(`qwen3:1.7b`, a small NPU/FastFlowLM reasoning model) — and confirmed the failure happened *after*
the browse-detection fix above was already deployed, so this was a new, different bug, not a stale
build.

Replayed the exact query through `app._web_search_context()` directly first, to isolate which half
was broken: detection correctly fired (`looks_like_browse_request` and `should_search_web` both
`True`), and the scrape correctly returned real content (NHS, MedlinePlus, and other medical
sources, fetched live). So the plumbing was never the problem — confirmed by feeding that exact
real context into a real completion against `qwen3.6-moe-35b-a3b` (the model used in most of this
session's other verifications), which answered correctly and used the data. Feeding the *identical*
context into `QwenFLM17B` specifically, though, reproduced the refusal exactly — a genuine
weak-model instruction-following gap, not a bug in this app's request-building or scraping code.
(A first attempt at reproducing this only gave the model a 250-token budget and got an empty
response — the same reasoning-budget trap as the MTP-model image-prompt-enhancement case study
earlier in this file. Re-run with real headroom, `qwen3:1.7b` reliably burns 1300+ characters of
`reasoning_content` before answering, and even then, the *final answer* was the refusal — a
distinct, separate problem from that earlier one, not the same bug recurring.)

**The fix**: the "you did browse for this" instruction was originally appended only *after* the
scraped content — reasonable for capable models, but confirmed live that a weak model can read
straight past a large block of injected content and still default to its trained refusal reflex by
the time it reaches a trailing instruction. Rewritten to lead with a short, direct, hard-to-miss
directive *before* the results too ("You DID just browse the web for this... Any restriction you
have against browsing/searching does NOT apply here"), in addition to keeping a reminder after.
Primacy mattered here, not just presence: the same content, only reordered, took `qwen3:1.7b` from
a flat refusal to *"Yes, I can search about pregnancy pain through the browser... I've used a real
headless Chrome browser to fetch them,"* unprompted. Costs nothing for stronger models that already
handled the trailing-only version fine (`qwen3.6-moe-35b-a3b` was never broken), so applied
unconditionally rather than gated on which model is loaded.

**Tested thoroughly for several use cases, per the explicit ask, all against the weak model that
actually failed** (not just the reported one, and not just the stronger model that was already
working) — three more real queries, each in a different topic and results shape: "browse the
internet and tell me the current price of Bitcoin" (a real price comparison table across
CoinDesk/CoinMarketCap), "can you browse and find out who won last night's game" (real, dated
sports scores across five different matchups), and "use a browser to search for a good chicken
curry recipe" (real recipe details with ratings and ingredients). All three answered correctly and
used the real scraped content — no refusals, on the exact model that previously failed on every one
of three real production attempts.

## Comparison tables in chat

When a model answers with a GFM-style pipe table (the standard Markdown table
format — a header row, a `---` separator row, then data rows — which is how
every model in this app naturally responds to "compare X vs Y"-style prompts),
the chat renders it as a real HTML `<table>` instead of showing raw
`| pipes | like | this |` text. Column alignment (`:---`, `---:`, `:---:`)
is respected. Each rendered table gets its own "Copy table" button in the
header, matching the existing per-code-block Copy/Download buttons — it
copies the table as tab-separated values (not raw Markdown), so pasting
straight into a spreadsheet (Excel, Sheets, Numbers) lands each cell in its
own column instead of dumping one blob of pipe-delimited text into cell A1.

Implemented entirely client-side in `static/app.js` (`parseTableBlock` /
`renderTableBlock`, wired into the existing `renderProse` markdown-lite
pass, alongside the pre-existing fenced-code-block handling) — no backend
changes, no new dependencies. Table parsing runs on the plain-text lines
*outside* of fenced code blocks only, so a pipe table pasted inside a code
fence (e.g. a markdown example) still renders as literal text, not as a table.

## RAG for Continue / OpenCode (codebase indexing — separate from cross-chat memory above)

Not to be confused with this webui's own cross-chat memory: this is about the *coding tools'*
codebase-search feature, documented in the Help panel's Continue section.

- **Continue** has real support for it (`@codebase` context provider): add a model entry with
  `roles: [embed]` pointing at `text-embedding-nomic-embed-text-v1.5` over the same Caddy `:8443`
  proxy already used for chat — no separate embeddings config block, Continue auto-selects whichever
  configured model has the `embed` role. Verified this actually works end-to-end (a real
  `/v1/embeddings` call through the Caddy proxy, 768-dim vectors back) before writing it into the
  Help panel. The embedding model needs to actually be *loaded* in LM Studio for Continue's direct
  requests to succeed — they go straight through Caddy to LM Studio, bypassing this app's
  `_ensure_embedding_model_loaded()` auto-load (that only fires on this app's own `/api/chat`).
  Load it once from the Models panel and it stays loaded (same "one model at a time" exemption as
  cross-chat memory above).
- **OpenCode** has no equivalent — checked its config schema and CLI directly rather than assume
  parity with Continue, and there's no embeddings/vector-index concept in either. It leans on
  agentic tool calls (grep/glob/read) to explore a codebase on demand instead of a pre-built
  semantic index. Documented as "not supported, and that's fine" in the Help panel rather than
  inventing a config block that wouldn't do anything.

## Debugging case study: Continue "not thinking through" Ryzen AI responses

A real report: Continue's responses via the Ryzen AI (Lemonade) model seemed shallow and not
properly following the actual ask. Worked through methodically rather than guessing at a fix:

1. **Checked whether Lemonade/FastFlowLM exposes a separate reasoning channel at all** — it
   doesn't. Confirmed live: neither top-level `"enable_thinking": true` nor the standard
   `"chat_template_kwargs": {"enable_thinking": true}` produced a `reasoning_content` field or
   `<think>` tags, on either the tiny `QwenFLM06B` (0.6B) or the full `qwen3.6-moe-35b-a3b`. This is
   a real gap relative to LM Studio (whose Qwen3.6 models stream `reasoning_content` cleanly) — but
   it turned out not to be the actual cause of the reported problem.
2. **Tested the model's actual capability directly**, bypassing Continue entirely: sent a
   multi-step coding instruction ("write a function, explain your approach, handle edge cases")
   straight to `qwen3.6-moe-35b-a3b`. It answered well — correct reasoning, correct code, sensible
   edge-case handling, both with `stream: false` and `stream: true`.
3. **Re-ran the identical test through the actual path Continue uses** — `https://localhost:8444`
   (Caddy, real bearer token), not `localhost:13305` directly — since a bug specific to the proxy
   layer wouldn't show up testing shortcut-direct. Same good result, streaming and non-streaming
   both. This ruled out Caddy, TLS, and auth as contributing factors.
4. **With the model and the transport both confirmed fine, the actual bug was in what this app was
   telling Continue about the model, not in the model or the connection.** Continue's config schema
   supports `defaultCompletionOptions.contextLength` to tell it how much context budget it has —
   and it was **missing from every single model entry** in the Help panel's Continue config, LM
   Studio's seven entries included, not just the one Ryzen AI entry. Without it, Continue likely
   falls back to a conservative built-in default and pre-truncates the system prompt / chat history
   / codebase context *before ever sending the request* — so no matter how capable the model is
   once it receives a full prompt, it never saw one. Separately, the one number that *was* present
   for the Ryzen AI entry (`context: 4096`, in the OpenCode JSON block) was stale: Lemonade now
   auto-tunes to `32768` regardless of what's requested at load time (confirmed live for both
   models via `GET /v1/health`'s `recipe_options.ctx_size`), so even that one specified number was
   wrong.

**Fix**: added `defaultCompletionOptions: { contextLength: 32768 }` to all 9 chat-role entries in
the Continue YAML config (not just Ryzen AI), and corrected the OpenCode JSON config's stale
`context: 4096` to `32768`. Also added a second Ryzen AI entry for `qwen3.6-moe-35b-a3b` alongside
the original `QwenFLM06B` smoke-test entry — the small model was only ever meant to prove the
integration worked, not for real use, and load history showed the bigger model is what's actually
being used day to day.

**Still an open, separate gap, not fixed**: Lemonade/FastFlowLM doesn't expose `reasoning_content`
at all right now, on any model tested. If Continue's UI is expected to show a distinct "thinking"
block the way it might for an Anthropic or LM Studio model, that won't happen for Ryzen AI
responses today — not a config problem, a real backend limitation. Worth re-checking after a
Lemonade/FastFlowLM update.

## Video generation (ComfyUI + Wan 2.1)

Direct requests such as **“create a short video of ocean waves”** are automatically routed from
the normal chat composer to a local text-to-video pipeline—there is no separate video button. It uses
ComfyUI's native nodes and the Apache-2.0 `Wan 2.1 T2V 1.3B` model, with the required UMT5 FP8
text encoder and Wan VAE. Results are saved under `data/videos`, rendered as an inline mobile-safe
HTML5 player, and include a **Download MP4** link. Media routes require the user's normal login;
deleting a conversation also deletes its generated video files.

This host runs ComfyUI with `--cpu`. Direct Wan generation proved unsuitable here in two real
tests: reduced settings produced unrelated blurred colors, while usable sampling settings took too
long. Natural-language video requests therefore use **fast coherent mode** by default: local
SD-Turbo creates a clear 512×512 AI scene, then local FFmpeg adds a smooth three-second camera move
and writes a mobile-compatible H.264 MP4. This is much faster and more recognizable, but it creates
cinematic pan/zoom motion rather than synthesizing independent object motion across frames. Wan
remains installed and visible in Models for future GPU use. The zoom is calculated on a 4096px
intermediate canvas before being reduced to 512px; this avoids the integer crop-position rounding
that made the earlier 640px animation visibly shake. Video requests bypass the separate LLM
prompt-enhancement pass and go directly to the media pipeline. Generation remains slower than
GPU-hosted video services. Advanced parameters are clamped to safe bounds in
`comfyui_client.generate_video()` to prevent one request from exhausting the host. The model files
live in ComfyUI's standard `models/diffusion_models`, `models/text_encoders`, and `models/vae`
directories; no remote generation service receives prompts or video data.

## Image generation (ComfyUI + SD-Turbo)

A second backend, unrelated to LM Studio: image requests typed into the normal composer and sent
are auto-detected and routed here instead of chatting (see "Automatic image-request detection"
below — there used to be a separate **Image** button, removed once detection existed). Backed by
[ComfyUI](https://github.com/comfyanonymous/ComfyUI) running as its own systemd service
(`comfyui.service`, `~/ai_projects/comfyui-env/ComfyUI`, `--cpu` — this machine has no discrete GPU
and no working ROCm runtime for the iGPU, only the pip wheel was ever installed, not the system
driver stack; bringing that up is a separate undertaking this didn't pursue) on port 8188, using
[`stabilityai/sd-turbo`](https://huggingface.co/stabilityai/sd-turbo) — a single-step-capable
model chosen specifically for CPU speed over SDXL-class quality. `comfyui_client.py` hardcodes one
minimal txt2img graph (checkpoint → CLIP encode → KSampler at `steps=2, cfg=1.0` → VAE decode →
save) rather than exposing ComfyUI's full node system, since this app only ever needs "prompt in,
PNG out". Measured 9-11s per 512×512 image on this CPU — much faster than expected for CPU-only
diffusion, though still not hosted-API speed.

`app.py`'s `/api/images/generate` saves the PNG to `data/images/<uuid>.png` and stores a message
whose content is plain `![prompt](/api/images/<uuid>.png)` markdown — deliberately reusing the
existing message pipeline rather than adding an image-specific schema. `renderProse()` in
`app.js` only matches that exact `/api/images/` path pattern (not arbitrary URLs), since there's no
use case here for embedding external images and it avoids any external-image-loading concerns.
Generated images are real files on disk, separate from PostgreSQL, so deleting a conversation or
a user also unlinks any image files their messages reference (`_delete_generated_images` in
`app.py`) — otherwise they'd accumulate forever after the conversation referencing them is gone,
the same class of cleanup gap the cross-chat memory feature above had to close.

```bash
systemctl --user status comfyui     # check status
systemctl --user restart comfyui
journalctl --user -u comfyui -f     # tail logs
```

### Composer UI stayed exactly as it was — controls were built, then rolled back

*(Historical: written while the composer still had a separate **Image** button, since removed —
see "Automatic detection replaced the manual toggles" further up. The point of this subsection —
extra controls were tried and deliberately rolled back — still stands.)*

A round of work added a gear (⚙) button next to Image with width / height / steps / seed fields,
plus a negative-prompt field, plus showing the used seed in the message meta line. All of it was
then removed at the user's request, in two steps: negative prompt first ("it doesn't work" — see
below for why), then the remaining width/height/steps/seed controls and the seed display right
after. Net effect: **the composer looks and behaves exactly like it did before any of this
section's work** — just Send and Image, no options panel, no seed shown anywhere. Worth recording
what was tried and why it came back out, so the same ground doesn't get re-covered blind later.

**Negative prompt**: the theory was sound (SD-Turbo runs at `cfg=1.0`, where a negative prompt has
zero effect by construction since the negative branch never gets blended in;
`comfyui_client.generate_image()` auto-bumps `cfg` to a mild `1.8` only when a negative prompt is
supplied, so it wasn't a no-op mechanically). In practice it still didn't produce usable results —
flagged directly by the user as "it doesn't work" after trying it. Most likely cause, consistent
with the risk called out when this was built: SD-Turbo is distilled *specifically* for
guidance-free sampling, and pushing `cfg` above 1.0 moves it outside the regime it was actually
trained for — at only 2-3 sampling steps there's very little denoising trajectory for negative
guidance to act on even before that.

**Width/height/steps/seed**: no reported bug here, removed anyway at the user's request right
after the negative-prompt field came out, along with the `seed <N>` shown in the message meta line.

**What's still true underneath, dormant but not deleted**: `comfyui_client.generate_image()` still
accepts `negative_prompt`, `seed`, `steps`, `width`, `height` as real parameters, still returns
`{image_bytes, seed, cfg}` rather than throwing the seed away, and the conditional-cfg logic is
untouched. `/api/images/generate` still accepts all of these in the request body — the composer
just never sends anything but `prompt` and `conversation_id` now. If a future model swap (a
non-distilled checkpoint that actually responds to CFG) or a different UI need makes any of this
worth re-exposing, the backend work doesn't need repeating, only the UI surface. Verified live
after each removal step: image generation keeps working normally with the reduced request shape
(cfg stays at the model's native 1.0, ~12-15s for a 512×512 image, matching pre-this-section
behavior).

**Submission and polling are now retry-tolerant.** Previously any single failed request — the
initial job submission, or one `/history` poll during the wait — aborted the whole generation
immediately. Job submission now retries up to 3 times with backoff (ComfyUI can be briefly
unresponsive right after its own restart, same reasoning as the LM Studio load-retry logic
elsewhere in this app); history polling now tolerates several consecutive failures in a row before
giving up, rather than one network hiccup killing a generation that's still running fine
server-side. A truly malformed workflow still fails immediately rather than retrying — that class
of failure won't succeed on attempt 2 either.

**GPU acceleration was investigated and deliberately not attempted.** This machine's iGPU
(`gfx1150`, same chip Lemonade's NPU integration already uses) is real ROCm hardware — AMD's
Ryzen-AI-specific ROCm docs confirm `gfx1150`/`gfx1151` support as of ROCm 6.4.4+. But that official
install path requires the **6.14 OEM kernel** (`linux-oem-24.04c`) with a reboot into it, while this
machine runs kernel **7.0** specifically because that's what Lemonade's XDNA2 NPU driver needed
(confirmed and verified working earlier). Downgrading the kernel to get ComfyUI onto the GPU would
directly risk breaking the already-working NPU integration for a modest speed gain (CPU generation
is already only ~10s for a 512×512 image at 2 steps) — flagged to the user rather than attempted
silently. If this is ever worth revisiting, it'd need a way to get ROCm's kernel requirement without
touching the NPU's, e.g. a second boot entry, not just following AMD's docs directly on this box.

### Automatic image-request detection + AI prompt enhancement

Previously the only way to generate an image was the explicit **Image** button — typing "draw me a
cat" and hitting **Send** just sent it to the chat model as text. `/api/chat` now auto-detects image
intent on every message and routes it into the image pipeline instead, so the button is now a
manual override rather than the only path in.

**Detection** (`comfyui_client.looks_like_image_request()`) is a keyword/regex heuristic in the same
style as `web_search_client`'s `looks_like_weather_query()` etc. — not real NLP, tuned against
concrete phrasings rather than guessed. "draw"/"paint"/"doodle"/"sketch" fire on their own;
"generate"/"create"/"make"/"design"/"render"/"illustrate"/"show me"/"give me" only fire when an
image-flavored noun (picture, photo, wallpaper, logo, portrait, poster, ...) shows up nearby, since
those verbs are just as often "generate a report" or "make a plan". A small idiom-exclusion list
stops "draw a conclusion/comparison/attention", "sketch out a plan/outline", etc. from false-
triggering — both very common in normal chat and sharing the same leading verb. Verified against 26
hand-picked positive/negative phrasings before wiring it in (all 26 passed):
`draw a cat`, `generate an image of a sunset`, `make me a wallpaper of...`, `give me an icon for...`
→ true; `generate a report`, `draw a conclusion`, `make a plan for the trip`,
`sketch out a rough plan for the launch`, `explain how image generation works` → false.

**Prompt enhancement** (`app._enhance_image_prompt()`) is the "more accurate" half: instead of
handing SD-Turbo the user's raw sentence verbatim, the already-loaded chat model rewrites it into a
proper txt2img prompt (comma-separated subject/setting/composition/lighting/color/mood/style
phrases) plus a matching negative prompt, grounded in the last few turns of conversation so a
follow-up like "make it a dog instead" inherits the scene established earlier. This is a real
accuracy improvement, not just formatting — SD-Turbo's tiny text encoder does much better with a
dense, structured prompt than with a casual "can you please draw me a..." sentence full of tokens it
has no use for. Fails soft on any error, empty response, or unparsable output: falls back to the raw
user text untouched, so a broken enhancement step degrades to today's old behavior rather than
blocking generation entirely.

**Debugging case study: enhancement silently did nothing for a reasoning model.** First live test
(`Jackrong-Qwopus3.6-35B-A3B-Coder-MTP-GGUF`, the MTP model from the section above) came back with
the enhanced prompt identical to the raw input — no error, just a no-op. Traced by capturing the raw
SSE stream directly: the model was emitting a full chain-of-thought into `reasoning_content` deltas
(~2600 characters, roughly 700+ tokens) before starting on the actual JSON answer in `content` — and
the enhancement call's `max_tokens=300` budget was being entirely consumed by reasoning before a
single token of real content ever arrived. Silent, not a crash: the fail-soft fallback caught the
resulting empty content and returned the raw prompt untouched, which looked like the feature simply
wasn't doing anything. Confirmed the fix two ways: raising the budget to `max_tokens=2000` let the
same request finish (reasoning + JSON both fit) and produced a real enhanced prompt; also made
`_extract()` check `reasoning_content` as a second-chance source if `content` comes up empty, for any
model that never cleanly separates the two streams. Any chat model capable of "thinking" before
answering will hit this same failure mode if the caller's token budget doesn't leave room for the
thinking — worth remembering for future features that do a quick behind-the-scenes model call like
this one.

**Verified end-to-end**, not just at the JSON-parsing layer: ran the full
detect → enhance → ComfyUI generate → save → persist pipeline directly (bypassing HTTP/session, same
verification style used elsewhere in this file) for `"generate an image of a small red fox sitting
in the snow at sunset, watercolor style"` — enhanced prompt came back detailed and on-topic, ComfyUI
produced a real 512×512 PNG, and the resulting image was visually inspected: a red fox sitting in
snow, warm sunset lighting, watercolor-ish texture — genuinely matching what was asked for, not just
"some fox image."

### Debugging case study: "image did not display" — the chat model hallucinating a fake image link

Reported right after the feature above shipped, with a real conversation: `"generate image of
hanuman"` correctly auto-routed and produced a real image. The user then said `"image is not
clear"` — a natural follow-up, but not a *new* image request by any reasonable reading, so
`looks_like_image_request()` correctly said no and it fell through to the normal chat model. That
model, having seen a real `![...](/api/images/<uuid>.png)` link earlier in the same conversation,
imitated the pattern in its own text reply — but invented a filename (`73b1234567890abcdef1234567890abc.png`,
confirmed not present in `data/images/`) since it has no actual way to create one. Rendered as a
broken image in the browser. Confirmed via the message history directly (`chat_history.db`): two
separate real occurrences, both `backend=lemonade` (the chat model), not `backend=comfyui` — proof
these were hallucinated text, not failed generations.

Two-part fix, not just a patch on the one phrase reported:

1. **`comfyui_client.looks_like_image_followup()`** — a deliberately broad revision/complaint
   vocabulary ("not clear", "blurry", "that's wrong", "try again", "higher resolution", ...) that on
   its own would false-trigger constantly in ordinary chat ("that's wrong, use a different
   approach"). It's therefore never trusted alone: `app._last_reply_was_image()` must *also* confirm
   the immediately preceding assistant turn was itself a generated image before the pair route to
   image regeneration. This turns "image is not clear" after a generated image into a real
   regeneration (`is_revision=True` tells `_enhance_image_prompt()` to treat the input as feedback on
   the previous prompt, not a fresh scene — it pulls the old prompt back out of the conversation
   excerpt and only changes what was flagged). Verified live: `"generate image of a tiger in a
   jungle"` → real image, then `"it's too blurry"` → correctly triggered a revision that kept the
   tiger/jungle subject and added `sharp focus, crisp, ultra-detailed, clear` — not a random new
   image.
2. **A system-prompt guard, as defense-in-depth** for whatever the keyword lists above still miss:
   every normal (non-image) chat turn now explicitly tells the model it cannot generate/attach images
   and must never write `![...](...)` markdown itself. Keyword lists are inherently incomplete;
   telling the model directly not to fabricate this specific pattern closes the gap for phrasings no
   regex was ever going to anticipate.

The two already-corrupted messages from before this fix (the ones with the fake `73b123...` and
`8412a128...` filenames) were replaced in the DB with a plain explanation, since they were never
recoverable — the "image" they claimed never existed.

## Debugging case study: long replies vanishing entirely, never saved

Reported alongside a real query ("browse all the deals for washing machine...") that appeared to
get no response at all. Traced via `chat_history.db`: the user's message was saved, but no
assistant reply ever appeared for it — not broken, not partial, completely absent, as if the
request had never happened.

Checked Lemonade's own journal for the same time window before assuming anything about this app's
own code: the request had, in fact, completed successfully server-side —
`Inference completed: model=qwen3.6-moe:35b-a3b, tokens=2287 (in=1484, out=803), ttft=15.481s,
tps=9.48`, a real ~96-second generation (search-augmented, longer response), with multiple
`POST /v1/audio/speech` calls in between confirming the reply was even being spoken aloud
sentence-by-sentence via the voice feature as it streamed. The model produced a complete answer.
It just never made it into the database.

Reproduced directly (not guessed at) using Flask's test client against the real `/api/chat` route,
with a real login session: fully consuming the response stream saved both messages correctly every
time. The bug only appeared when the *client* stopped reading partway through — abandoning the
response after just the first SSE chunk (simulating a disconnected browser: backgrounded tab,
network hiccup, laptop sleep, anything that can plausibly happen over a 90+ second wait) meant the
assistant's reply was generated in full but never saved, reproducing the report exactly.

**Root cause**: the old `generate()` function's database save lived in a `finally` block, *after* a
`yield` — the trailing "tops" SSE chunk sent once generation finishes. That generator is *pulled* by
Flask/Werkzeug on behalf of the client; if the client has already disconnected by the time that
yield executes, writing to the dead connection fails and raises right there, which — because it's a
plain `finally` block executing top-to-bottom — skipped every line after it, `chat_store.add_message()`
included. The actual LLM response was fully collected in local variables by that point; it just
never got written anywhere before the exception cut the block short. Not a rare timing accident:
any reply now taking 90+ seconds (common for search/scrape-augmented queries against a real model)
was one client-side hiccup away from silent, total data loss — which was exactly the profile of the
query that surfaced it.

**Fix**: decoupled stream consumption and the database save from the client connection entirely.
`backend.chat_stream()` is now consumed by an independent background thread (`threading.Thread`,
daemon) that pushes each line onto a `queue.Queue`; the Flask-facing `generate()` function does
nothing but pull from that queue and `yield` to the client. The worker thread's own
`for line in backend.chat_stream(...)` loop — and everything in its `finally` block, save included —
runs to completion regardless of whether anything is still reading from the queue on the other end.
A client disconnecting can now only ever cost the client its own live view of the reply; the save
itself is no longer reachable by that failure at all.

Verified both directions, not just the fix in isolation: (1) simulated the disconnect scenario
exactly as it was reproduced above — read only the first chunk of a real streamed response, then
abandoned it entirely — and confirmed the full reply still saved correctly after the fact; (2) fully
consumed a normal response and confirmed the SSE stream is byte-for-byte the same shape as before
(`[DONE]` then the trailing `tops` chunk, in order), so this is a pure reliability fix with no
change to the client-visible protocol.

## Voice: mic input and spoken responses

Asked for voice chat "just like ChatGPT" with an "exact human voice." Both halves of that framing
needed scoping down to what's actually achievable locally before building anything, rather than
building toward a target this hardware and these open models can't hit and calling it done anyway:

- **"Real-time" here means turn-based, not continuous/interruptible.** ChatGPT's Advanced Voice Mode
  uses a specialized always-on audio-native model with sub-second latency and mid-sentence
  interruption ("barge-in"). What's actually running here is three separate local stages in
  sequence — Whisper (speech-to-text) → the chat LLM → Kokoro (text-to-speech) — each with real
  processing time on this hardware. That's a click-to-record, speak, get-a-spoken-reply-back
  experience (closer to a walkie-talkie / push-to-talk voice assistant), not a live phone
  conversation. Nothing about local STT→LLM→TTS pipelines makes true full-duplex realistic on a
  single Ryzen AI laptop chip today.
- **"Exact human voice" isn't available from a local, free/open TTS model**, and this app doesn't
  use paid cloud voice APIs (consistent with its no-cloud-API-keys rule elsewhere — ddgs and
  Open-Meteo over paid alternatives, this app's whole own image/chat stack running local models).
  [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) (the TTS model used here) is a genuinely
  good, natural-sounding open-source voice — not robotic, not the flat text-to-speech people
  associate with older assistants — but it is a synthetic voice, not indistinguishable from a
  specific real person.

**What's built, both verified working end-to-end on this hardware**:
- **Mic input**: a 🎤 button in the composer. Click to start recording (`MediaRecorder` in the
  browser), click again to stop — the recording is posted to `/api/voice/transcribe`, transcribed
  via Whisper, and the resulting text auto-fills the composer and auto-submits (matching the
  voice-assistant flow this was asked to mirror: you speak a complete message and stop, the same as
  releasing a push-to-talk button, rather than needing to also press Send).
- **Spoken responses**: a 🔊 toggle in the composer (persisted in `localStorage`, off by default).
  When on, every completed assistant reply is posted to `/api/voice/speak`, synthesized via Kokoro,
  and auto-played. Markdown is stripped first (`stripMarkdownForSpeech()` in `app.js`) — code
  fences, image markdown, link syntax, bold/italic markers, and header hashes are removed or
  unwrapped so Kokoro isn't asked to read `"asterisk asterisk bold asterisk asterisk"` aloud.

**`lemonade_client.py` additions**: `transcribe_audio()` and `synthesize_speech()`, both against
Lemonade's OpenAI-compatible `/v1/audio/transcriptions` and `/v1/audio/speech` — confirmed live via
a full local round-trip (`synthesize_speech("...")` → real MP3 bytes → `transcribe_audio()` on that
same MP3 → the original text back out correctly) before any frontend code was written. Both models
live in their own Lemonade resource slots (`"transcription"` and `"tts"`, separate from `"llm"`/
`"embedding"` per `/v1/health`'s `max_models`) and load lazily on first use, same pattern
`_ensure_embedding_model_loaded()` in `app.py` already uses — loading either one never evicts
whatever chat model is currently loaded, confirmed live.

**Two real compatibility bugs found and fixed before this worked at all, not assumed away**:
1. **Whisper's default backend crashes on this machine.** `Whisper-Tiny` failed to load with the
   exact same crash signature as the GPU incidents documented elsewhere in this file
   (`whisper-server process has terminated with exit code: 134`) — traced to Lemonade defaulting
   Whisper to the ROCm backend, which (like ComfyUI and llama.cpp's GPU path) doesn't actually work
   on this machine's driver setup. Fixed two ways: installed the working CPU backend
   (`lemonade backends install whispercpp:cpu`, run as the `lemonade` system user), and confirmed
   the load request needs `whispercpp_backend: "cpu"` as a **top-level** field on the `/v1/load`
   payload — a nested `recipe_options.whispercpp_backend` (the pattern every other per-recipe
   option in this file uses) was silently ignored, and it kept defaulting back to the broken ROCm
   backend until the field was moved to the top level.
2. **Whisper-server rejects the audio format browsers actually send.** Confirmed live: posting a
   real WebM/Opus file (Chrome's `MediaRecorder` default, with no format specified — i.e. exactly
   what this app's own mic button produces) got a flat `400 Invalid request`; the same audio
   converted to 16kHz mono PCM WAV was accepted and transcribed correctly. `_convert_to_wav()` in
   `lemonade_client.py` shells out to `ffmpeg` (a new system dependency, documented in Setup above)
   before every transcription request, so the frontend can stay simple — record in whatever format
   the browser defaults to, let the backend handle compatibility.

**Verified end-to-end, the same real-pipeline discipline as everything else in this file**: not
just each piece in isolation, but the full path a real mic recording actually takes — a WebM/Opus
file synthesized from a real TTS clip (standing in for a browser recording, since this environment
has no physical microphone to record from directly) round-tripped through `ffmpeg` conversion →
Whisper transcription → back to the original text correctly. The `/api/voice/transcribe` and
`/api/voice/speak` routes were confirmed registered and `@login_required`-protected (401 without a
session, consistent with every other API route in this app).

### Local Gemma voice translation

The composer also has a target-language selector and a 🌐 translation microphone. It records one
utterance, converts the browser's WebM/Opus recording to 16 kHz mono WAV, and sends an OpenAI-style
`input_audio` block to the already-installed `gemma4-it:e4b` model through FastFlowLM. Gemma 4 E4B
runs on the Ryzen AI NPU and includes its own 913 MB audio encoder; the complete local model occupies
about 8.5 GB. The translated text is shown in chat and spoken through the existing local Kokoro TTS
model. No recording, transcript, or translation is sent to Gemini or another cloud service.

The `/api/voice/translate` route is authenticated, caps recordings at 15 MB, validates the target
language, and holds the shared NPU lock for the whole inference. A real English audio sample was
translated to Spanish as “¿Dónde está la estación de tren más cercana?” in about five seconds after
model selection. The ordinary 🎤 button remains Whisper dictation; 🌐 specifically invokes Gemma
voice translation.

The translation selector also supports all four major South Indian languages: Tamil, Telugu,
Kannada, and Malayalam. Their native names are shown in the selector, while the stable English
language name is sent to the backend. Targets are checked against a server-side allow-list before
the model prompt is built.

### Follow-up: hands-free voice conversation mode via client-side VAD

Feedback on the click-to-record mic button: it types/sends correctly, but "it's not automatic like
phone conversation or live conversation" — every turn still needed a manual click to start
recording. True full-duplex (talk over the assistant, interrupt it mid-sentence) genuinely isn't
achievable with a local sequential STT→LLM→TTS pipeline, as already scoped in the section above.
But the actual friction being described — having to click a button for every single turn — has a
real, buildable fix that doesn't need a different backend architecture: **voice activity detection
(VAD)**, done entirely client-side.

A new 📞 "conversation mode" button in the composer starts a fully automatic loop, no clicking
required once started: listen (via the Web Audio API's `AnalyserNode`, measuring live microphone
volume — no model or network round-trip needed just to detect "is someone talking," pure signal
processing in the browser) → auto-detect when the user stops talking (volume drops and stays below
a threshold for 1.2s, after at least 300ms of actual speech was heard first, so a brief pause
mid-sentence doesn't cut the recording early) → auto-transcribe → auto-send → speak the reply →
once playback finishes, automatically start listening again. A 30-second safety cap force-ends an
utterance that never goes quiet (a stuck/noisy mic shouldn't record forever).

Implementation notes:
- `speakText()` (used by the manual 🔊 toggle too) was changed to return a Promise that resolves
  only once audio playback actually finishes, not just once it starts — conversation mode `await`s
  this before resuming listening, which is also what prevents the classic voice-assistant "hears
  itself" bug: the mic stream stays open throughout (so there's no re-permission prompt or gap
  between turns), but the VAD loop is only running while in the `listening` state, never while the
  assistant's reply is actively being spoken.
- The existing one-shot 🎤 mic button and the new 📞 conversation mode share the same underlying
  `/api/voice/transcribe` endpoint and the same auto-send-via-`requestSubmit()` mechanism — the
  loop is just what decides *when* to stop recording and *whether* to restart listening afterward,
  not a separate transcription/send path.
- `getUserMedia` now requests `echoCancellation`/`noiseSuppression`/`autoGainControl` explicitly
  (shared `MIC_CONSTRAINTS`, used by both the one-shot button and conversation mode) — standard
  browser-level audio cleanup that meaningfully helps transcription accuracy, essentially free to
  request.

Still turn-based under the hood — each cycle still pays real STT/LLM/TTS processing time, so there's
a "thinking" pause each turn, same as the section above already set expectations for. What changed
is that the *rhythm* is now hands-free and automatic (talk, pause, listen to the reply, talk again),
which is what the "not automatic" feedback was actually about, rather than sub-second latency or
mid-sentence interruption, neither of which this hardware can do locally regardless of UI polish.

### Follow-up: spoken replies had a long silent gap after the text finished

Reported: "reply back voice takes more time even after chats are printed" — the text appears fully,
then nothing happens for a long time before the assistant starts speaking. Measured directly rather
than guessed at: `synthesize_speech()` against a realistic ~1300-character reply (a normal answer
length in this app) took **23.6 seconds** before returning any audio at all. Root cause: Kokoro runs
on CPU here (its default backend) and synthesizes at roughly 55-60 characters/second — and the old
`speakText()` sent the *entire* reply as a single synthesis request, so the whole thing had to finish
generating before a single word could play. A short reply was fine; a normal-length one had a
20+ second dead-silence gap, exactly matching the report.

**Fix**: `speakText()` now splits the reply into sentence-sized chunks (`splitIntoSpeechChunks()`,
merged up to ~200 characters so short sentences don't each pay their own request overhead) and
pipelines synthesis with playback — chunk 2's synthesis request is kicked off as soon as chunk 1's
*audio* comes back (not after chunk 1 finishes *playing*), so it's normally sitting ready by the
time chunk 1's playback ends. This works because synthesis reliably outpaces natural spoken
playback on this machine: a 146-character chunk synthesizes in ~2.5s (confirmed live) but takes
roughly 10+ seconds to actually speak aloud at natural pace, leaving comfortable headroom for the
next chunk to be ready in time. Time-to-first-audio for that same ~1300-character reply dropped
from 23.6s to ~2.5s — the length of one sentence's synthesis, not the whole reply's, regardless of
how long the full response is.

No backend changes were needed — `/api/voice/speak` already accepted arbitrary text per request;
this is purely a client-side orchestration change in how many times it's called and when. The
`await speakText(...)` contract conversation mode depends on (resolve only once everything has
finished playing, so it doesn't start listening again mid-reply) is preserved: the chunk loop still
awaits every chunk's playback in order before returning, it just no longer blocks on synthesizing
the *entire* reply up front before starting the *first* one.

### Follow-up 2: still silent — because speech only started after generation finished, not just after synthesis

The chunked/pipelined fix above was real, but the report continued: "still a bit more silent... can
you talk while streaming text." Correct diagnosis on the user's part — the previous fix only sped up
*synthesis*, but `speakText(assistantText)` was still only ever called once, after the SSE stream
that carries the LLM's response had entirely finished. For a slow model or a long response, the
model's own generation time (independent of TTS entirely) could itself be many seconds to a minute+
of silence before speech synthesis even started.

**Real fix**: speech is now driven directly off the token stream, not off the completed message.
`enqueueSentencesFromStream()` runs inside the SSE loop in `app.js`, right after every `delta` is
appended to `assistantText` — it finds newly-completed sentences (ending in `.`/`!`/`?`/newline)
beyond a `spokenUpTo` cursor and queues each one for synthesis the instant it completes, while the
model is still generating the rest of the reply. A new `speechQueue` (`enqueueSpeech`/
`runSpeechQueue`/`waitForSpeechQueueDrain`) replaced the old fixed-array chunk loop from the first
fix, since sentences now arrive one at a time over the reply's whole lifetime rather than all being
known upfront — it keeps the same synthesis-while-playing pipelining as before (prefetches the next
queued sentence's audio while the current one plays), just fed incrementally instead of from a
pre-split list.

**One correctness issue this surfaced immediately, before it shipped**: sentence boundaries don't
respect Markdown structure, and this app is used constantly for coding help (see e.g. the MTP-vs-NPU
and Playwright sections' own transcripts). A response streaming through a ` ```python ` code block
would complete a "sentence" *inside* the block (a period inside a comment, a blank line) before the
closing fence had even arrived — queuing that fragment would read raw code syntax aloud.
`isInsideCodeFence()` tracks running backtick-fence parity and `enqueueSentencesFromStream()` simply
declines to queue past an unclosed fence, leaving that whole block (code and all) to be picked up by
the final flush once the reply completes and the fence has closed — verified directly (simulated the
exact streaming/chunking logic against a real code-block response in Python, word-by-word, mirroring
how SSE deltas actually arrive): the two prose sentences before the code block were queued and
spoken immediately as they streamed in, the code block was correctly held back untouched until its
closing fence arrived, and the final flushed chunk containing the complete block passed through
`stripMarkdownForSpeech()`'s existing code-fence stripping cleanly, leaving only the prose after it.

The two changes compose: sentences are queued as early as the *model* allows (as soon as each one
finishes generating), and each queued sentence is synthesized as early as *Kokoro* allows (prefetched
during the previous sentence's playback) — closing both gaps the user identified across the two
reports, not just the one this section originally fixed.

## Download Models page

A dedicated admin page (`/downloads`, linked from the chat header next to Models/Users/Settings)
for finding and downloading new models — separate from the Models panel, which is now just for
managing what's already on disk (load/unload/delete). Three sections, because the two backends
genuinely offer different capabilities here, not because of an arbitrary split:

- **Model search (GGUF, via Ryzen AI / Lemonade)** — real, structured search, and genuinely
  comprehensive, not just Lemonade's own local list. Downloading a result goes through
  `pull_catalog_model()` (local catalog id) or `pull_huggingface_repo()` (arbitrary HF repo) via
  `POST /api/models/download-catalog`, both yielding *structured* byte-level progress
  (`bytes_downloaded`/`bytes_total`/`percent` per file, confirmed genuinely available from
  Lemonade's own SSE stream) — driving a real progress bar, not just a log.
- **LM Studio** — no search, by design: checked directly before building anything (no `lms
  search` subcommand exists, no undocumented catalog/search HTTP endpoint responds either — only
  `/api/v0/models`, which lists local downloads, not a remote catalog). Same "type a name, it
  resolves and downloads the best match" flow as before, just moved to this page.
- **Ryzen AI NPU (FastFlowLM) — exact tag** — also moved here, unchanged in behavior. Not folded
  into the catalog search above because FastFlowLM's NPU model list is a genuinely separate
  catalog this app has no OS permission to read directly (`flm list` needs root — confirmed live:
  even this app's own service user gets a flat permission-denied on the binary itself).

**Search gap found and fixed same day: "glm 5.2" returned nothing.** Real user report.
`lemonade_client.search_catalog()` only filters Lemonade's own ~111-entry curated catalog by
substring — confirmed live it has exactly one GLM entry at all (`GLM-4.7-Flash-GGUF`), so any
newer/less-common model was never going to be found there, not a bug in the filtering itself.
Root cause traced by inspecting Lemonade's own desktop app directly (`curl` its `renderer.bundle.js`
and grep for how its search box actually works) rather than guessing: it calls Hugging Face's
public model-search API straight from the browser
(`https://huggingface.co/api/models?search=...&filter=gguf&sort=downloads`, no key needed).
Added `search_huggingface()` doing the same thing, merged into `/api/models/search`'s results for
any non-empty query — confirmed live this alone fixed the reported case (0 → 12 real results for
"glm 5.2", including the actual `unsloth/GLM-5.2-GGUF` repo). Empty-query browsing (the page's
initial load, before typing anything) intentionally skips the live search and stays local-catalog
only, so the page still opens fast.

Also confirmed live, not assumed: Lemonade can download an arbitrary Hugging Face GGUF repo, not
just entries in its own catalog — the same custom-registration shape the NPU download path already
used (`{"model_name": "user.<slug>", "checkpoint": "<repo>:<file>", "recipe": "llamacpp"}`) works
for any HF repo:file pair, tested end-to-end against a real small repo outside the catalog. Since
this app has no per-file quantization picker UI, `_pick_gguf_file()` auto-selects a sane default
(Q4_K_M and similar mid-quality quants preferred, in the order the llama.cpp community generally
treats as sensible defaults; mmproj/vision-projector files excluded; falls back to the smallest
`.gguf` file found) — same "auto-pick rather than prompt" simplicity `lms get -y` already uses for
LM Studio. Once downloaded this way, the model registers under `recipe: "llamacpp"`, making it
indistinguishable from a native catalog entry on future searches (no stale-duplicate risk).

**Metadata**: size, last-updated date, likes (Hugging Face's closest thing to a star rating — it
has no formal review/star system), download count, and a real description are now shown per
result. Hugging Face's API has no free-text description field at all (confirmed live: no `expand`
value for one exists, `cardData` is structured metadata only) — `_extract_readme_description()`
pulls the first substantial prose paragraph out of the model's actual README instead, skipping
YAML frontmatter, code fences, headers/badges/images, bare links, and common "join our Discord"
CTA boilerplate that many model cards lead with. Iterated on this against several real READMEs
until it reliably found genuine content (e.g. correctly extracts GLM-5.2's actual intro paragraph
despite it being preceded by four different community-link paragraphs and followed by a BibTeX
citation block) rather than shipping the first version that merely didn't crash. Local catalog
results get the same treatment via `enrich_with_hf_metadata()`, resolving each entry's checkpoint
back to its Hugging Face repo — only Lemonade's own catalog lacks these fields natively; Hugging
Face search results carry size/likes/downloads/date inline from the search response already, no
extra enrichment call needed for those.

Verified end-to-end through Flask's test client with a simulated admin session (not just each
piece in isolation): the page loads, a real search returns real results, and a full download
against an actual small catalog model (`RealESRGAN-x4plus-anime`, 17MB) streamed correct
byte-level progress through to completion and showed up as `"downloaded": true` on that model
afterward — then cleaned up via delete, confirming the state flips back correctly too.

**Follow-on bug, same day: "not all downloaded models are visible in the Models page."** Real
user report, and a direct consequence of the Download Models page above making it possible to
pull arbitrary non-flm (llamacpp) models for the first time. `lemonade_client.list_models()`
(what actually populates the Models panel — a different function from the catalog *search* above)
only ever included two kinds of entry: `recipe == "flm"` ones (always, regardless of load state)
and *currently loaded* ones (a safety net). Any non-flm model that was downloaded but not
currently loaded fell through both — confirmed live with two real, genuinely-downloaded models
(`Qwen3-Coder-30B-A3B-Instruct-GGUF`, downloaded via the Download Models page itself, and
`nomic-embed-text-v1-GGUF` while idle) that were completely absent from the Models panel despite
`downloaded: true` in the raw catalog. This mattered less before today, since the only non-flm
custom download was the embedding model, which stays loaded most of the time — the loaded-only
safety net usually papered over it.

**Fix**: the primary loop now also keeps any entry with `downloaded: true`, regardless of recipe
— confirmed live (multiple real pulls/deletes) that unlike flm, `downloaded` genuinely is
reliable for llamacpp entries; it's the same field the Download page's own search already trusts
for its "downloaded" badge. One thing this touched that needed care: that loop had always
hardcoded `"type": "llm"` for every entry, which was harmless while only flm (always chat models)
passed through it — now that the embedding model can flow through this same loop when unloaded,
that hardcoding would have silently reintroduced the "embedding model shows up in the chat
dropdown" bug fixed earlier. Fixed by reading the catalog entry's own `labels` field (`labels:
["embeddings"]`, confirmed present there even though the catalog entry has no `type` field the
way `/v1/health`'s *loaded* entries do) instead of assuming "llm". Verified live end-to-end
through the real `/api/models` route with simulated admin and non-admin sessions: both previously
-missing models now appear (admin sees both, non-admin correctly still doesn't see the embedding
one), and loading/chatting with the newly-visible Coder model was unaffected.

**Third same-day follow-on: "download page is not showing current downloading models."** Real
user report. Root cause: a download's progress had only ever lived inside the SSE stream of
whichever request started it — reload the Downloads page mid-download, or open it in a second
tab, and there was no way to know one was even running, even though it kept going server-side
regardless (Lemonade doesn't stop a pull just because the client disconnected). Checked whether
Lemonade itself exposes a "downloads in progress" status endpoint before building anything:
`/api/v1/downloads` and `/v1/downloads` both exist and return `200 []` rather than 404, which
looked promising — but polled repeatedly against a real multi-second download in progress and it
stayed `[]` the entire time. Not usable; whatever that endpoint tracks, it isn't `/v1/pull`
streams.

**Fix**: tracked entirely in this app instead. A small in-memory registry in `app.py`
(`_active_downloads`, a plain `dict` + `threading.Lock` — safe because the service runs
`app.run(..., threaded=True)`, one process with shared memory across threads, not multiple
workers, so no external cache/queue is needed for something this small and short-lived) is
updated by both download-streaming routes (`/api/models/download` for the freeform LM
Studio/NPU-tag path, `/api/models/download-catalog` for the structured catalog/Hugging Face path)
as progress events flow through, and cleared when a download finishes or errors. A new
`GET /api/models/downloads/active` endpoint exposes a snapshot of whatever's currently running,
which `downloads.js` now polls every 2s — if a poll finds something and this tab isn't already
the one streaming it (`busy` false), it renders the same progress bar/log a live SSE stream would
have shown, from whatever the last known snapshot was. Verified with the actual failure scenario,
not just the individual pieces: two independent Flask test-client sessions (simulating two
browser tabs/logins that share nothing except the server process), one starts a real download,
the other — which never touched the download endpoint at all — polls and correctly sees it in
progress, then correctly sees it clear the moment it completes.

### Follow-up: a search box for every backend, and mandatory sizes

The page now also starts with **Search all backends**, which queries LM Studio, Lemonade,
vLLM, KoboldCPP, and FastFlowLM together and returns the same normalized fields for every
card: backend, format, execution compatibility, size, task type, update date, popularity,
download state, and a verified source link. Backend-specific sections remain available for
focused browsing.

LM Studio search-card downloads use the result's exact
`https://huggingface.co/<owner>/<repo>` URL and `lms get --gguf -y`. Passing the bare repo id
to `lms get` invokes its fuzzy catalog resolver and was observed sitting indefinitely in its
animated resolution UI; the full URL is the CLI's documented direct-Hugging-Face path and
was verified through Hearth's SSE download endpoint against an installed model.

The three-section split above (real search for Lemonade only; LM Studio and FastFlowLM as
plain "type a name/tag" forms) was closed out — **every backend now has a card-based search on
the Downloads page**, all rendering through the same `renderResults()` cards and
`/api/models/search?backend=...` endpoint:

- **LM Studio** and **KoboldCPP** search the public **Hugging Face GGUF index** (the same
  source Lemonade's GGUF search already used, `filter=gguf&sort=downloads`), because both
  install GGUF repos straight from Hugging Face — LM Studio via `lms get` (freeform text-log
  download), Kobold by fetching the `.gguf` and auto-picking a balanced quant (structured
  byte-level download). This replaced LM Studio's old "no search, by design" note: it has no
  *catalog* API of its own, but it doesn't need one to search the same HF index everything
else does. The by-exact-name / full-HF-URL box is kept below the results as a fallback.
- **FastFlowLM** searches its own fixed **NPU catalog** — `flm list --filter all --json`, which
  (unlike the earlier "needs root" finding for the *NPU* itself) runs fine as this user for a
  plain listing. `fastflowlm_client.search_catalog()` filters it client-side by
  tag/family/params/quant/label, shows each model's real on-NPU footprint as its size, marks
  already-`installed` tags as downloaded, and sorts installed-first then smallest-first. Its
  empty query loads the whole catalog to browse; the by-exact-tag box is kept as a fallback.
- **Mandatory sizes.** GGUF search results used to show "size varies by quantization" (or
  nothing) because a repo holds many quants. `lemonade_client.fill_result_sizes()` now fills a
  **real** size for every result — the size of the *default quant* the download would actually
  pull (`_pick_gguf_file`), or the full repo size for non-GGUF — concurrently (12 workers) and
  cached, so a search of ~25 results sizes them all in ~2s and repeats are instant. Verified
  100% coverage live (e.g. `qwen coder` → 12/12 sized, `llama` → 25/25), so the Downloads page
  always shows a size before you commit to a download. Only a genuine HF lookup failure renders
  as an honest "size unknown".

The cross-tab active-download poller (`/api/models/downloads/active`) refreshes whichever
backend's grid a finished download belonged to (all five backends now).

## Ryzen AI NPU backend (Lemonade Server)

A third backend, alongside LM Studio and ComfyUI: [Lemonade Server](https://github.com/lemonade-sdk/lemonade)
(`lemonade_client.py`), AMD's own local LLM server, adds NPU-accelerated inference on this
machine's XDNA2 NPU (Strix Halo, "Ryzen AI 9 HX 470") on top of the GGUF/CPU/iGPU path LM Studio
already covers — specifically **NPU+GPU hybrid execution** (NPU handles prefill/time-to-first-token,
GPU handles sustained decode), which is how this chip's NPU and iGPU compute actually combine for a
faster response, rather than using either engine alone.

This wasn't the obvious choice — AMD's official "Ryzen AI Software" toolkit (raw XRT + XDNA driver
+ ONNX Runtime/VitisAI EP, distributed as a `.tgz` from a gated developer portal) is the
lower-level, more commonly-documented path, but it ships no chat server at all (you'd write the
serving loop, tokenizer handling, and KV-cache management yourself) and its hybrid NPU+GPU
execution provider has historically been Windows-first. Lemonade, by contrast, was confirmed (via
live search while building this — see below) to be, as of **March 2026**, *"the first working
end-to-end path for LLM inference on AMD Ryzen AI NPUs under Linux"* (Lemonade 10.0 + FastFlowLM
runtime) — and it's already an OpenAI-compatible HTTP server, so integrating it is the same shape
as `lmstudio_client.py`, not a from-scratch inference stack.

**Installed and verified end-to-end** (`sudo add-apt-repository ppa:lemonade-team/stable && sudo apt
install lemonade-server`, runs as the system service `lemond.service`, listening on
`http://localhost:13305` — `LEMONADE_BASE_URL` to override). `system-info` confirms real hardware,
not assumed: XDNA2 NPU, **58 TOPS int** measured (the 86 TOPS figure floated earlier was off — 58 is
what Lemonade's own detection reports for the NPU alone), 12-core/24-thread CPU, `gfx1150` iGPU with
2GB VRAM. Two backends installed for the hybrid path: `lemonade backends install flm:npu` (FastFlowLM,
NPU) and `lemonade backends install llamacpp:vulkan` (GPU, same Vulkan path LM Studio already uses on
this iGPU — avoids the ROCm gap noted in the ComfyUI section above). AMD's own `ryzenai-llm` NPU
recipe reports `"state": "unsupported", "message": "Requires Windows"` on this machine — confirming
FastFlowLM really is the only working NPU path on Linux right now, not just the easier one.

**A real gotcha worth documenting**: Lemonade's built-in model registry (`lemonade list`, `GET
/v1/models`) is GGUF-only — it carries no FastFlowLM/NPU models at all. NPU models are a separate,
manual pull using FastFlowLM's *own* short tags (not a Hugging Face repo id):

```bash
sudo /var/lib/lemonade/.cache/lemonade/bin/flm/npu/flm list      # e.g. qwen3:0.6b, llama3.2:1b, gemma3:4b
lemonade pull user.MyModel --checkpoint main qwen3:0.6b --recipe flm
```

Worse: once pulled, these `user.*` FLM models **don't appear in plain `GET /v1/models`** — a real
user report caught this directly: unloading one made it vanish from the Models panel entirely
instead of showing "not loaded" the way LM Studio models do. First fix attempt only checked
`GET /v1/health`, which folded loaded models back in but didn't solve unloaded ones being invisible
in the first place. The actual fix: `strace`-ing the `lemonade` CLI's own `list` command (rather
than guessing further) showed it calls `GET /v1/models?show_all=true` — an undocumented query param
that returns Lemonade's *entire* catalog, custom models included, regardless of load state. Its
per-model `downloaded` field is unreliable for flm models though (`false` even for ones confirmed
on disk via `du -sh`), so `lemonade_client.list_models()` instead filters on `recipe == "flm"` —
reliable because Lemonade ships no built-in flm catalog entries of its own (confirmed live: every
non-custom entry is llamacpp/sd-cpp/etc.), so any flm-recipe entry is one this app's Ryzen AI
download path actually pulled. Verified live: loaded `QwenFLM06B`, unloaded it, confirmed via
`/api/models` that it stayed listed as `"state": "not-loaded"` instead of disappearing, then
reloaded it successfully.

Also note: the *id* to use for chat/load/unload is the bare `model_name` (e.g. `QwenFLM06B`), not
the `user.`-prefixed name used to pull it (e.g. `user.QwenFLM06B`) — the two differ, and only the
bare form actually routes correctly.

**Models panel is now grouped by backend** — a real second ask alongside the disappearing-model
bug report. `/api/models` tags every entry with `"backend": "lmstudio"` or `"backend": "lemonade"`
(`app.py`, right where the two lists get merged — the client modules themselves stay
backend-name-agnostic), and the panel renders two headed sections instead of one flat mixed list.

**This bit the Models panel's download UI directly** — the "Ryzen AI" download option originally
called Lemonade's plain `POST /v1/pull {"model_name": ...}`, which only resolves GGUF catalog
names; a real report of pasting an LM Studio-style id (`qwen/qwen3.6-35b-a3b`) there came back with
*"the model name must include the `user` namespace"*, since Lemonade was trying to register it as a
new custom model. Fixed in `lemonade_client.search_and_download()`: it now always registers a
`user.<slug>` custom model with `{"checkpoint": <tag>, "recipe": "flm"}` — confirmed against the
live server, since this exact JSON shape isn't in the public REST docs, only the CLI's
`--checkpoint`/`--recipe` flags are. The download box's hint text now says to use an exact FastFlowLM
tag (`qwen3:0.6b`, from `flm list` above), not a Hugging Face repo id or an LM Studio catalog name.

Verified with a real round trip through this app's own `/api/chat` (not just curling Lemonade
directly): pulled `qwen3:0.6b` via FastFlowLM, loaded it onto the NPU, sent a chat message through
the live app, got a correct streamed answer, and confirmed it persisted to `chat_store` with the
right model name, duration, and token counts — same as any LM Studio response.
**~57 tokens/sec decode, ~204 tokens/sec prefill** on this 0.6B model on the NPU, from the
response's own `usage` block (`decoding_speed_tps` / `prefill_speed_tps` — Lemonade reports these
directly, no need to time it externally).

`lemonade_client.py` mirrors `lmstudio_client.py`'s interface (`list_models`, `load_model`,
`unload_model`, `chat_stream`, `search_and_download`) but talks to Lemonade's own REST API instead
of shelling out to a CLI — Lemonade, unlike LM Studio, exposes model load/unload/pull over plain
HTTP (`POST /v1/load`, `/v1/unload`, `/v1/pull`), so no CLI dependency exists for this backend at
all. `app.py` routes `/api/chat` and load/unload by checking `lemonade.owns_model(<id>)` (a live
lookup against Lemonade's own model list) rather than a single hardcoded model id the way Colibri
used to be checked — Lemonade can hold many models, not just one. Loading a Lemonade model doesn't
trigger this app's usual "unload every other LM Studio model first" policy — same independent-
resource-pool treatment Colibri got, since it's a separate process with its own memory/NPU budget.

**One real limitation, unlike LM Studio's fuzzy `lms get <query>` search**: neither Lemonade's
catalog nor FastFlowLM has a free-text search endpoint — the Models panel's download box needs an
*exact* FastFlowLM tag (a dropdown next to the query box switches which backend's catalog you're
searching; see the gotcha below for exactly where those tags come from and why a Hugging Face repo
id or LM Studio catalog name won't work there).

The Models panel's "quantization" column is repurposed for Lemonade entries to show which chip a
model actually runs on (`NPU`, `NPU + GPU`, `GPU`, or `CPU`) instead of a quant string — more
useful here, since *which engine it targets* is the entire point of this backend existing
alongside LM Studio.

**LAN access**: like LM Studio, Lemonade doesn't listen beyond this machine on its own — it goes
through Caddy the same way, on its own port (`:8444`, reusing the slot Colibri's proxy block used
to occupy, `/etc/caddy/Caddyfile`), same shared CA cert and bearer token as LM Studio's `:8443`.
Confirmed live: unauthenticated requests to `:8444` get a real `401`, authenticated ones reach
Lemonade correctly. The Help panel's Continue and OpenCode config blocks each got a second
entry/provider pointing at `:8444` alongside the existing LM Studio one, so both tools can reach
either backend from the same machine. Cross-chat memory (this app's own RAG) works identically
regardless of which backend answers chat, and now runs its embedding through Lemonade's own
`POST /v1/embeddings` (`nomic-embed-text-v1-GGUF`) rather than LM Studio's — see "Cross-chat
memory" above for why. Continue's `@codebase` RAG (a separate, Continue-side feature) still points
at LM Studio's bundled embedding model as documented in the Help panel; unaffected by that switch
since it's Continue talking directly to LM Studio, not something this app routes. OpenCode still
has no RAG/embeddings concept at all regardless of backend (see the RAG section above) — nothing
to add there for Lemonade either.

## vLLM backend (CPU-only, )

A fourth backend, alongside LM Studio, Lemonade, and ComfyUI: [vLLM](https://github.com/vllm-project/vllm)
(`vllm_client.py`), added with a real architectural difference from the other two chat backends —
vLLM itself has **no runtime load/unload API**. A `vllm serve <model>` process is started once and
serves exactly that model until the process itself is restarted with a different `--model`; there's
no `POST /v1/load` the way Lemonade has, nor an `lms load` CLI the way LM Studio has. First shipped
as routing/Settings/Caddy plumbing only (`list_models`, `owns_model`, `chat_stream`, with
`/api/models/load|unload|delete` returning a clear 422 for any vLLM-owned model id rather than
silently misrouting it to LM Studio's code path). Full model management — search, download,
load, unload, delete, matching Lemonade's UX exactly — was built on top afterward; see the
follow-up case study below for how, given vLLM has none of this itself.

**LAN access via Caddy**: same pattern as the other two, a new block on `:8445` (`/etc/caddy/Caddyfile`)
proxying to `localhost:8000` with the same shared bearer token and CA cert. Verified live: no/wrong
token gets a real `401`; a correct token got a `502` (Caddy reachable, nothing yet listening on 8000)
right up until the service below actually started, at which point it started returning real model
data — confirming the proxy layer itself was never the issue, only what was or wasn't behind it.

### Debugging case study: getting a real vLLM server running on this hardware at all

Initially wired up as client/routing/Settings/Caddy plumbing only, pointed at `localhost:8000` with
no real server behind it (a deliberate first phase — see the git-free nature of this repo, changes
ship incrementally). Getting an actual model serving turned out to be the harder half, for reasons
specific to this exact machine:

**Plain `pip install vllm` is a dead end here, and fails immediately, not at runtime.** The default
PyPI wheel targets NVIDIA CUDA — its dependency graph pulls in `torch` with a hard
`cuda-toolkit==13.0.2` pin, `cuda-bindings`, `tilelang` with `humming-kernels[cu13]`, and more. On
this AMD-iGPU machine (no NVIDIA GPU at all), `pip install vllm --dry-run` failed at dependency
*resolution* itself — `ResolutionImpossible`, `cuda-tile[tileiras]` requiring a `cuda-toolkit` version
range with literally no matching distribution for this environment. Not a "wrong device, falls back
to CPU at runtime" situation — the install never gets that far.

**Real CPU support exists, and — better than expected — doesn't require a source build.** Cloned
vLLM's own repo to check its actual current CPU docs
(`docs/getting_started/installation/cpu.x86.inc.md`) rather than trusting older training-data memory
of the process, since a from-scratch build (`VLLM_TARGET_DEVICE=cpu`, pinned toolchain, uncertain
success) was the expected fallback. It documents a **pre-built CPU wheel** published directly on each
GitHub release: `vllm-X.Y.Z+cpu-cp38-abi3-manylinux_2_34_x86_64.whl`, installable straight via
`pip install <url> --extra-index-url https://download.pytorch.org/whl/cpu` — no compiler, no
`git clone`, no build step. `requirements/cpu.txt` in the source tree confirmed the real dependency
(`torch==2.11.0+cpu`, no CUDA anywhere) before trusting the docs' claim. This CPU's flags
(`avx512f`, `avx512_vnni`, `avx512_bf16`) exceed the doc's own recommendation over plain `avx2`, and
vLLM even auto-selects a Zen-specific `ZenCpuPlatform` (ZenDNN-optimized kernels) on AMD Zen CPUs
with AVX-512 — genuinely well-matched hardware for this path, unlike the Vulkan/GPU instability
documented elsewhere in this file for other backends.

**The wheel itself didn't run out of the box.** First launch (`vllm serve Qwen/Qwen2.5-0.5B-Instruct
--port 8000`) crashed during engine startup:
`AttributeError: '_OpNamespace' '_C' object has no attribute 'init_cpu_memory_env'`, preceded by
`Failed to import from vllm._C: ImportError('...cannot enable executable stack as shared object
requires: Invalid argument')`. Root-caused via `readelf -l` on the actual `.so`: its `GNU_STACK`
program header was marked `RWE` (executable stack) instead of `RW` — a missing `-z noexecstack`
linker flag from the wheel's own build, and this kernel refuses to `mmap` an executable-stack shared
object rather than silently allowing it. Ubuntu no longer ships the classic `execstack` tool to fix
this (package doesn't exist in this repo), but `patchelf` 0.18+ does:
`patchelf --clear-execstack <file>.so`. Three files in the wheel needed it —
`vllm/_C.abi3.so`, `vllm/_C_AVX2.abi3.so`, `vllm/_C_AVX512.abi3.so` (found by scripting
`patchelf --print-execstack` over every `.so` under `site-packages/vllm/`, not guessed) — after which
the model loaded, warmed up, and served a real streamed chat completion successfully. This patch
lives only in the installed venv's site-packages, so **a future `pip install --upgrade vllm` would
silently reintroduce the crash** until the same three-file patch is reapplied; not automated into a
post-install hook since upgrades here are expected to be manual and infrequent for a smoke-test model.

**Deployed as `vllm.service`** (`/etc/systemd/system/vllm.service`, system-level like `lemond.service`,
not a user unit like `ai-webui.service`) — `Restart=on-failure`, `LD_PRELOAD` set for TCMalloc +
Intel OpenMP (the docs' own performance recommendation for the CPU backend), `VLLM_CPU_KVCACHE_SPACE=2`
(GiB, deliberately small — this is a smoke-test model, not sized for real concurrent load), serving
`Qwen/Qwen2.5-0.5B-Instruct` at `--max-model-len 4096`. Verified live end-to-end after `systemctl
enable --now`: real chat completions via `curl` directly against `:8000`, through Caddy's `:8445`
with the bearer token, through this app's own `vllm_client.chat_stream()`, and through `/api/models`
showing up correctly tagged `"backend": "vllm"` — not just "the process started," an actual reply was
generated and returned through every layer.

**Chosen model is explicitly a smoke test, not a coding option** — 0.5B, CPU-only, `--max-model-len
4096` — same category as Lemonade's `QwenFLM06B` (0.6B) elsewhere in this file: fast enough to prove
the whole stack (Caddy → vLLM → this app → Continue/OpenCode) actually works end-to-end, not
competitive for real generation quality or speed. The NPU path (Ryzen AI / Lemonade, above) remains
the actual fast/capable local backend on this hardware; vLLM's real value here is proving the fourth
backend's plumbing is correct, ready for a larger model or a real GPU host later if one becomes
available. Help panel's Continue/OpenCode config examples were updated with this real model id
(previously placeholder/empty, since no server existed yet) and its exact `contextLength: 4096`,
matched to `vllm.service`'s `--max-model-len`, not guessed.

### Follow-up: full search/download/load/unload/delete, matching Lemonade's UX

Real request: bring vLLM to the same operational parity as Lemonade in the Models/Downloads
panels, "by running the CLI commands behind it." The genuine obstacle is the one already
documented above — vLLM has no HTTP load/unload API at all, so unlike wrapping Lemonade (already
an HTTP server) or LM Studio (already has an `lms` CLI for exactly this), there was no existing
mechanism to wrap. Built one:

**The key design decision: move `vllm.service` from a system unit to a *user* unit
(`~/.config/systemd/user/vllm.service`, `systemctl --user`, no `sudo`) before writing any code
that controls it.** The service was originally set up as a system unit (mirroring `lemond.service`,
running as a dedicated `lemonade` system user) when it was still a fixed, hand-configured smoke
test with nothing dynamic about it. Once the plan became "the always-on Flask app itself restarts
this service on every Load click," that stopped being the right shape: embedding `sudo systemctl
restart <service>` calls into a persistent, LAN-reachable web server is a real privilege-escalation
surface (any bug elsewhere in that app becomes root-adjacent). Since `ai-webui.service` itself
already runs as a **user**-level systemd unit under the same account, converting `vllm.service` to
also be a user-level unit means the app controls it with exactly the privileges it already has —
no sudo, no new attack surface, verified directly by reading `ai-webui.service`'s own running
process's `/proc/<pid>/environ` for a real `DBUS_SESSION_BUS_ADDRESS`/`XDG_RUNTIME_DIR` (confirming
`systemctl --user` calls from inside that process reach the same session as issuing them manually)
rather than assuming this "should" work. `loginctl show-user` confirmed `Linger=yes` was already
set for this account (from `ai-webui.service` itself needing to survive logout/reboot), so the
converted unit persists identically.

**Model/context/dtype live in an env file** (`~/.config/vllm/service.env`, `EnvironmentFile=` in
the unit, `ExecStart=/bin/bash -c 'exec vllm serve "$VLLM_MODEL" --max-model-len "$VLLM_MAX_MODEL_LEN"
--dtype "$VLLM_DTYPE" ...'`) — `vllm_client.load_model()` rewrites this file and runs `systemctl
--user restart vllm`, then polls `/v1/models` until it reports the *new* model specifically (not
just "the service is up," which briefly reports nothing mid-restart and could otherwise return a
stale in-flight response). This is the entire mechanism behind Load: there's no vLLM-side API being
called at all, just a config rewrite plus a restart, confirmed by timing a real cycle
(`unload_model` → `load_model` on the same cached model: **48.2s**, matching the model-warmup time
seen in the original smoke test — i.e. genuinely the same cold-start cost, not a shortcut).

**A small local registry** (`~/.config/vllm/models.json`) tracks which Hugging Face repos this app
has explicitly downloaded/loaded, rather than trusting the shared `~/.cache/huggingface/hub`
directory wholesale — checked directly and confirmed this matters: that cache already held an
unrelated, incomplete repo (`mateogrgic/GLM-5.2-colibri-int4-with-int8-mtp`, an empty `snapshots/`
dir, likely a stray partial download from something else entirely) that `huggingface_hub`'s own
`scan_cache_dir()` correctly excludes as invalid — but a *complete*, valid, unrelated download by
some other tool wouldn't be excluded, and would have shown up as a false "vLLM-manageable" model
without this registry. `search_huggingface()` (new, in `vllm_client.py`) hits the same public HF
API Lemonade's own equivalent function does, `filter=text-generation` instead of Lemonade's
`filter=gguf` since vLLM serves whole causal-LM repos directly, no GGUF quant-file picking needed.

**Download** shells out to the `hf` CLI (ships with `huggingface_hub`, already a vLLM dependency —
no extra install) rather than calling `huggingface_hub.snapshot_download()` directly, matching the
existing "run the CLI commands behind it" pattern `lmstudio_client.search_and_download()` already
uses for `lms get` (same ANSI-strip/dedup-consecutive-lines progress parsing, reused directly,
since `hf download`'s tqdm bars redraw the same way). One real dependency gap this surfaced:
`huggingface_hub` was only ever installed in the separate vLLM venv
(`vllm-server/.venv`) — but `vllm_client.py` runs *inside the Flask app's own process*, which has
its own separate venv that never had it. `delete_model()` failed with a plain `ModuleNotFoundError`
on first real test until `huggingface_hub` was added to the app's own `requirements.txt` and
installed into `ai-webui/.venv` too — a reminder that "the vLLM machinery has this dependency" and
"the code calling into it has this dependency" are two different requirements when they live in
separate virtualenvs, confirmed by the test actually failing, not assumed fixed by installing it
in the other venv.

**Delete** unloads first if the target is the currently-served model (same reasoning as
`lemonade_client.delete_model()`), then removes both the registry entry and the actual cached
weights via `huggingface_hub.scan_cache_dir().delete_revisions(...)`. Verified end-to-end with a
real throwaway repo (`hf-internal-testing/tiny-random-gpt2`): downloaded it, confirmed it appeared
in `/api/models` as `"state": "not-loaded"`, deleted it, confirmed it disappeared from both the
app's list *and* `scan_cache_dir()`'s view of the actual HF cache — while the real Qwen model
stayed completely untouched throughout.

**Downloads page** gained a third search section ("vLLM (Hugging Face)") alongside the existing
Lemonade-catalog and LM Studio-by-name sections — `renderResults()` was generalized to take a
target container + a download callback instead of being hardcoded to Lemonade's, so the same
card-rendering code serves both without duplicating it. The "someone else's in-flight download,
observed from a different tab" polling path (`pollActiveDownloads`, see the Download Models page
section below) also had to learn to refresh the *right* backend's results on completion — it
previously always refreshed Lemonade's, which would have shown nothing changing after a vLLM
download finished if observed from a second tab.

Verified the whole surface end-to-end through the real HTTP-shaped API (Flask `test_client`, not
just calling `vllm_client` functions directly): search returns real Hugging Face results with an
accurate `downloaded` flag, download streams real progress and registers the model, the Models
panel lists it as `not-loaded`, load restarts the service and confirms the *new* model is being
served, delete removes it from both the registry and disk. Then confirmed the one piece that
couldn't be proven from a plain Python shell — that `systemctl --user` calls actually work from
*inside* the systemd-managed `ai-webui.service` process, not just from an interactive login shell —
by re-running `vllm_client` calls under `env -i` reconstructed from that exact running process's
own `/proc/<pid>/environ`.

## Whisper load failure, and keeping RAG/voice models warm

Real user report: "failed to load whisper llm". `/api/models/load` for `Whisper-Tiny` returned a
502 wrapping Lemonade's own `500` from `/v1/load`. Lemonade's logs (`sudo journalctl -u
lemond.service` — a **system** unit, same `--user`-scope trap as `flm.service` earlier: checking
it with `--user` silently shows nothing rather than erroring) showed the real cause:
`whisper-server process has terminated with exit code: 134` (SIGABRT), instantly, on every retry.
Running the binary manually surfaced the actual error under that: `libamd_comgr.so.3: failed to
load library` — a ROCm runtime library that exists on this machine only inside Ollama's private
bundled ROCm directory (`/usr/local/lib/ollama/rocm_v7_2/`), not on the standard library search
path, so whisper.cpp's ROCm build can't find it and aborts immediately. Consistent with this
machine's iGPU (gfx1150, Strix Point/RDNA3.5) already being a known-unsupported ROCm target (see
the vLLM section above).

A CPU `whisper-server` variant was already installed alongside the broken ROCm one. Rather than
touching system-wide ROCm library paths, changed Lemonade's own global `config.json`
(`whispercpp.backend: "auto"` → `"cpu"`) and restarted `lemond.service` — clean load in ~1s
afterward. Interestingly, `lemonade_client._ensure_voice_model_loaded()` already had a *per-call*
`whispercpp_backend: "cpu"` override forcing this same fix, from an earlier session — but only for
the lazy on-first-use voice code path, not the generic Models-page "Load" button
(`lemonade_client.load_model()`), which is the path that actually failed here. The global config
change now covers both.

**Also addressed while fixing this**: the embedding (RAG/cross-chat memory), STT (Whisper), and TTS
(kokoro-v1) models were all lazy-load-on-first-use only — reasonable for keeping a cold app light,
but it meant every fresh `ai-webui.service`/`lemond.service` restart left RAG and voice cold until
whatever request happened to need them first, eating that load latency (or, before the fix above,
outright failing). Two changes:

1. Discovered Lemonade's `pinned` per-model setting isn't settable via the `/v1/load` API's request
   body (accepted with `status: success` but silently had no effect — confirmed via `/v1/models`'s
   `recipe_options` staying `{}` afterward) — same "silently ignored" trap as the nested
   `whispercpp_backend` form documented in `_ensure_voice_model_loaded()`'s docstring. It only takes
   effect via Lemonade's `recipe_options.json` file directly, keyed by the bare model id (not the
   `builtin.<id>` prefix its one existing entry uses — that prefix turned out to be specific to
   Lemonade's own curated/builtin catalog models, not a general convention). Set
   `{"Whisper-Tiny": {"whispercpp_backend": "cpu", "pinned": true}, "kokoro-v1": {"pinned": true},
   "nomic-embed-text-v1-GGUF": {"pinned": true}}` — confirmed live via the per-model settings log
   line (`pinned=true`) and `/v1/models`'s `recipe_options` field, protecting all three from
   Lemonade's idle-timeout auto-eviction (`evict_idle_timeout=300`) once loaded.
2. Added `_warm_up_rag_and_voice_models()` in `app.py`, run in a background thread right after
   `app.run()` is called (so a slow/unreachable Lemonade never delays this process binding its own
   port) — proactively loads all three via the same `_ensure_embedding_model_loaded()`/
   `_ensure_voice_model_loaded()` functions the lazy paths already use, each independently
   fail-soft. Verified end-to-end: a full restart of both `lemond.service` and `ai-webui.service`
   from a clean/unloaded state resulted in all three showing `loaded` with zero user interaction.

## Models page: cold-start feedback

Real user report: "model load is failing" for vLLM. Direct testing (stopping vllm.service, then
calling `/api/models/load` for real) showed the opposite — it succeeded every time, auto-starting
the stopped service correctly, same as KoboldCPP and FastFlowLM already did. The actual problem was
in `models.js`: `toast()` unconditionally auto-hid after 4 seconds regardless of how long the
operation actually took, and nothing else on the page changed until the whole request resolved. A
cold vLLM start took a genuine 91 seconds in testing — so for ~87 of those seconds, the page looked
completely idle with no toast, no card change, nothing — indistinguishable from a hang. `toast()`
gained a `persist` flag; `loadModel`/`unloadModel`/`deleteModel` now show a toast that ticks up every
second ("...42s elapsed") for the whole operation, and the specific model's card shows a pulsing
"Loading…"/"Unloading…"/"Deleting…" badge with its buttons disabled (previously only a module-level
`busy` flag silently no-op'd extra clicks, with no visible reason why). Verified with a real headless
browser click-through of an actual ~90s vLLM cold start: the toast was confirmed still visible and
correctly counting up at the 10s/30s/50s/70s marks (all well past the old 4s cutoff), then flipped
to the loaded state cleanly at completion, with zero JS errors.

## Tool-calling for OpenCode: FastFlowLM can't, vLLM can

Real user report: "OpenCode not working on FastFlowLM/LAN". Diagnosed by sending the exact request
OpenCode sends — a `tools`-bearing chat completion — directly at each backend's real LAN Caddy
endpoint (not localhost, to rule out the proxy layer). Chat itself worked fine everywhere (200s in
7-10s); the actual gap was **tool-calling**. OpenCode is agentic: every request carries function
definitions (`bash`, `read`, `edit`, …) and it needs the model's reply to come back as a structured
OpenAI-format `tool_calls` array, not prose, so it has something to execute.

**FastFlowLM (both `llama3.2:1b` and `qwen3.6-moe:35b-a3b`) accepts the `tools` parameter but never
returns `tool_calls`** — it just answers in plain text/markdown ("You can use `ls -l`…"). This is a
genuine runtime limitation, not a bug in this app or a Caddy misconfiguration: the OpenAI-compatible
surface FLM exposes doesn't implement the tool-calling half of the spec.

**vLLM does**, once told to: `vllm.service`'s `ExecStart` (`~/.config/systemd/user/vllm.service`)
gained `--enable-auto-tool-choice --tool-call-parser hermes`. `hermes` matches the ChatML-style
function-calling format Qwen models are trained on — the fixed choice is safe here the same way
`--language-model-only` already is (see the vLLM section above): every model in this app's registry
is Qwen-based or GGUF (GGUF is already refused by `check_servable`), so one parser covers the whole
servable set without needing to be per-model configurable. Also switched the served model from
`Qwen2.5-0.5B-Instruct` to the already-downloaded `Qwen2.5-3B-Instruct` — small enough for fast CPU
inference, official Qwen tool-calling support.

Verified live, both non-streaming and streaming, through the real LAN path (`https://<ip>:8445/v1/chat/completions`,
Caddy TLS + bearer auth, not localhost): a `tools`-bearing request now comes back with
`"finish_reason": "tool_calls"` and a real `tool_calls` array (`{"name": "bash", "arguments":
"{\"command\": \"df -h\"}"}`); streaming mode fragments the same call correctly across SSE deltas
(name first, then argument chunks keyed by `index`), which is what OpenCode's SSE parser expects.

`help_data._pick_default()` already prefers whichever chat model is currently *loaded* (to avoid
OpenCode's first request paying a cold-load penalty) — so as soon as vLLM was serving
`Qwen2.5-3B-Instruct`, the auto-generated OpenCode config's default model flipped to
`vllm/Qwen/Qwen2.5-3B-Instruct` at `https://<lan-ip>:8445/v1` with no code change needed there.
Re-copy the config from the Help panel to pick this up.

Net effect at the time: FastFlowLM/NPU was the right choice for plain chat but not OpenCode — see the
follow-up immediately below, which found and fixed the actual root cause.

### Follow-up: FastFlowLM tool-calling actually fixed via a small shim

The "FastFlowLM can't" conclusion above turned out to be half right: FastFlowLM's server-side output
*parsing* for tool calls is genuinely complete and correct — the missing piece was upstream of that,
in what it does with an incoming request. Confirmed by reading the model's own
`~/.config/flm/models/*/chat_template.jinja` directly rather than guessing: it has full tool-calling
logic (Qwen's own format — `<tool_call>\n<function=NAME>\n<parameter=P>value</parameter>\n</function>\n
</tool_call>`), rendered from a `# Tools\n\n...` system block whenever the template's `tools` Jinja
variable is populated. FastFlowLM's server evidently never populates it from the request's `tools`
field — so the model never sees the block and has no idea it's supposed to answer that way, and just
answers in prose instead.

**The fix turned out not to need any output-side parsing at all.** Manually constructing that exact
system-block text (copied verbatim from the template) and sending it as a literal system message —
with the request's `tools` field left out entirely, since FLM ignores it either way — was enough:
FastFlowLM's own response handling correctly detected the model's `<tool_call>` output and returned a
proper structured `tool_calls` array on its own, in both streaming and non-streaming mode, and
correctly consumed a `tool` role follow-up message (the result being fed back) on a second turn to
produce a real final answer. Verified directly against FastFlowLM's raw API (bypassing Caddy) before
touching anything else, to isolate this from any proxy-layer possibility.

**Shipped as `flm_tool_shim.py`**, a small reverse-proxy Flask app (its own
`flm-tool-shim.service`, `127.0.0.1:52626`) sitting between Caddy and FastFlowLM's real server
(`127.0.0.1:52625`): a `/v1/chat/completions` request carrying `tools` gets that one system message
injected before being forwarded; everything else (including `/v1/models` and every other path) is
proxied through byte-for-byte unmodified — the shim never touches a response. One more real bug
surfaced while testing this shim through the *actual* Caddy LAN path with OpenCode's real generated
config: OpenCode's config uses this app's own `flm/qwen3.6-moe:35b-a3b` id (the `flm/` prefix
`fastflowlm_client.py` uses to disambiguate FastFlowLM's tags from Lemonade's overlapping ones,
per that module's docstring) as the literal `model` field — sending that prefixed string straight to
FastFlowLM (which expects the bare tag) didn't error, but produced *badly* malformed tool-call XML
(missing closing tags, spurious fields) where the bare tag reliably produced correct output. The
shim strips the prefix before forwarding, same as `fastflowlm_client._tag()` already does for this
app's own (non-shimmed) calls to FLM.

Caddy's `:8447` block (`reverse_proxy @authorized localhost:52625`) was repointed at the shim
(`localhost:52626`) — validated with `caddy validate` before `systemctl reload caddy` (a reload, not
a restart, so it never drops the connections other LAN clients already had open). Verified with the
exact same test suite that first caught the failure, now run three times against the real LAN path
(`https://<ip>:8447`, TLS + bearer auth): auth enforcement, non-streaming tool calls, streaming tool
calls (`finish_reason: tool_calls`, correctly fragmented SSE deltas), and the full
call → tool-result → final-answer round trip, all passing against `qwen3.6-moe:35b-a3b` — the exact
model and exact LAN path the original bug report was about. FastFlowLM's own
`fastflowlm_client.py` (used by this app's own /api/chat, which never sends `tools`) is untouched by
any of this — it still talks to FastFlowLM on :52625 directly.

Two follow-on bugs, both caught by actually using the shim through OpenCode rather than stopping at
the tool-calling test suite:

**Real user report right after landing this: OpenCode still produced degenerate garbage output**
(this time repeated `?`/fragment tokens, not the earlier `/` corruption). Cause: `fastflowlm_client.
chat_stream()` already sends `frequency_penalty=0.3` as a repetition guard for this app's own calls
(a fix from earlier the same day — quantized NPU models on this backend reliably degenerate into a
token-repetition loop past a few hundred tokens with no penalty at all). But OpenCode's requests go
through the shim, which never touches `fastflowlm_client.py` at all — so they got no penalty and hit
the exact same failure mode again, just via a different path than the one already fixed. `flm_tool_
shim.py` now applies the same `frequency_penalty=0.3` default (only if the caller didn't already set
one) before forwarding. Verified with a 500-token generation through the real LAN path — clean
markdown throughout, zero degeneration — plus a full re-run of the tool-calling suite to confirm the
penalty doesn't interfere with tool-call detection.

**Also found while testing this specific fix**: the port-to-backend map both `app.py`'s
`_read_caddy_info()` (Settings page's "Test via Caddy" button) and `help_data.parse_caddyfile()`
(Help panel's port table / generated configs) use to recognize which Caddy block is which backend is
keyed on the *upstream* port number — and both only knew about `52625` (FastFlowLM's raw server), not
`52626` (the shim Caddy now actually points `:8447` at). Repointing Caddy silently broke both of
those, surfacing as "No fastflowlm proxy block found in Caddyfile." Fixed by having both maps treat
either port as "this is the fastflowlm block."

**A third bug, worse than the first two**: real user report — a live OpenCode reply came back
visibly corrupted mid-sentence (coherent text, then a stray `</think>`, then garbage tokens like
`__`/`—`). Confirmed directly from `flm.service`'s own request/response logs (`journalctl -u
flm.service`), which capture the exact raw request OpenCode sent and FLM's raw generated text —
this is what made the diagnosis possible instead of guesswork: `qwen3.6-moe:35b-a3b` is a
*thinking* model, and its chat template (like the "Tools" block above) forces every assistant turn
to open with a `<think>\n` block by default. On a tool-result-summarization turn deep in a real
multi-turn tool-calling conversation (the user had OpenCode run a `wifi_devices.py` network-scan
script, then asked it to summarize the results), the model produced a complete, correct answer,
then appeared to get confused about where its own `</think>` closing marker belonged — emitting it
*after* the real content instead of before, followed by garbage. `finish_reason` was `stop`, not
`length`, confirming this was the model breaking format, not hitting a token cap — i.e., a genuinely
separate failure mode from the `/`-corruption and `?`-repetition bugs above, not a variant of either.

Confirmed FastFlowLM honors the standard `enable_thinking: false` request field (verified directly
against its raw API): with it set, the model's template maps to a pre-closed empty think block
instead of an open one, and the reply comes back completely clean, no `<think>` framing at all.
Since OpenCode's tool-loop doesn't need a visible reasoning trace anyway, `flm_tool_shim.py` now
defaults `enable_thinking: false` on every request unless the caller already specified one — this
avoids the entire failure class up front rather than trying to detect and strip corrupted output
after the fact. Verified by replaying the actual failing scenario (the same wifi-scan tool result,
fed back for summarization): a clean, complete answer with a proper markdown table and a coherent
follow-up question, zero stray tags — plus a full re-run of the tool-calling test suite (still
16/16) to confirm disabling thinking doesn't interfere with tool-call detection.

## KoboldCPP and FastFlowLM backends

Two more local backends were added, each behind its own authenticated Caddy port and each with
the **same Search / Download / Load / Unload / Delete lifecycle and LAN API surface as Lemonade**
(so they appear as first-class providers in the auto-generated Continue / OpenCode configs — see
Help). This brings the backend count to five.

**KoboldCPP** (`kobold_client.py`, a llama.cpp server) serves **one GGUF at a time** and has no
runtime load API of its own — so, exactly like vLLM, this app builds one on top:

- Runs as a **user** systemd unit (`kobold.service`) reading `~/.config/koboldcpp/service.env`,
  which pins the served GGUF path + model name + context. "Load" rewrites that env file and
  `systemctl --user restart kobold`; "download" fetches a GGUF from Hugging Face
  (`hf_hub_download`, balanced-quant auto-pick) and registers it in a small id↔path registry
  (`koboldcpp/<gguf-basename>` ids). "Delete" removes the registry entry (and unloads first if it
  was the served one).
- LAN: Caddy `:8446` → `localhost:5001`, same bearer token / CA cert as the others.
- Download search reuses the Hugging Face GGUF index (see the Downloads-page search follow-up
  above).

**FastFlowLM** (`fastflowlm_client.py`) is the AMD Ryzen AI **NPU** runtime run **standalone**
(`flm serve`, OpenAI-compatible on `127.0.0.1:52625`), rather than through Lemonade:

- FastFlowLM and Lemonade drive the **same NPU** and only one `flm` process can hold it at a
  time, so this backend and Lemonade's NPU models are **mutually exclusive** — the load endpoint
  in `app.py` frees the other before activating this (`_free_npu_except` / `_lemonade_is_npu`).
  FLM tags (`llama3.2:1b`, `deepseek-r1:8b`, …) overlap Lemonade's flm-recipe ids, so this app
  namespaces them with an `flm/` prefix for unambiguous routing (stripped when talking to FLM).
- The `flm serve` process must run as a **system** unit (`flm.service`) because the NPU needs
  raised memlock, which a `--user` unit can't grant (confirmed: a user unit's
  `LimitMEMLOCK=infinity` still resolved to 8192). The app controls that system unit **without a
  password** via a polkit rule (`/etc/polkit-1/rules.d/50-flm.rules`) — start/stop only. `flm
  list` / `flm pull` run the binary directly as this user (no NPU, no memlock needed).
- LAN: Caddy `:8447` → `localhost:52625`.
- Download search hits FLM's own fixed NPU catalog (`flm list`), see the Downloads-page follow-up
  above.

Both are wired into every shared code path that previously special-cased backends: the model
list (`_all_models` in `help_data.py`, `/api/models`), the load/unload/download/delete endpoints,
the download workers (structured for both), and the help-config generators.

## Performance metrics board

An admin-only metrics page (`/metrics`, `templates/metrics.html` + `static/metrics.js`, gated by
`admin_required`) capturing **every inference request across all entry points** — webui chat,
OpenCode, and VSCode/Continue — with response time, request/ask type, tokens generated,
throughput, and NPU TOPS.

- **In-app chat** rows are recorded directly in the chat worker (`metrics_store.record(...)`,
  `source="webui-chat"`) with exact tokens, tok/s, TOPS, backend, model, response time, and an
  inferred *ask category* (coding / reasoning / general / …).
- **LAN tool** rows are attributed primarily through `X-Hearth-Client` and secondarily
  through User-Agent detection. Lemonade and FastFlowLM traffic passes through inline shims
  that record model, token usage, throughput, NPU status, and streaming content.
  Backends without an inline shim are captured by `caddy_metrics.py`, a background tailer
  over Caddy's JSON access log (`/var/log/caddy/access.jsonl`), with the fields visible at
  the proxy layer.
- Everything lands in a `request_metrics` table (`metrics_store.py`); `summary(hours)` aggregates
  by source / backend / ask-type over a selectable window (last hour / 24h / 7d / all time),
  surfaced via `GET /api/metrics` and rendered as KPI chips, a timeline, per-dimension bar
  breakdowns, and a recent-requests table.

### Follow-up: paginating and filtering the recent-requests table

The recent-requests table was originally an unconditional `ORDER BY id DESC LIMIT 60` baked
into `summary()` itself — a real gap once actual usage accumulated: it ignored the page's own
time-range selector entirely (switching to "Last hour" changed every KPI and chart except the
table sitting right below them), had no way to see anything past the newest 60 rows, and no way
to narrow down to one backend/model/status while investigating something.

Split into its own `metrics_store.recent(hours, page, page_size, source, backend, request_type,
ask_category, status_class, model)` and `GET /api/metrics/recent` — decoupled from `summary()`
so turning a page or changing a filter doesn't re-fetch/re-render the whole KPI dashboard (and
the 15s auto-refresh doesn't reset whatever page/filter view an admin is mid-way through). Filter
dropdowns (source/backend/request type) are populated from `DISTINCT` values actually present in
the selected time window, not a hardcoded list, so a backend nothing has hit yet in "Last hour"
just doesn't show up as an option; status filters into ok/client-error/server-error buckets
(matching the table's own existing color-coding) rather than exact HTTP codes, and the model
filter is a debounced substring match. Verified live against the real accumulated data
(137 rows): pagination correctly returns distinct pages in order, a backend filter narrows the
count and resets to page 1, and clearing filters restores the full unfiltered view.

### Follow-up: a live "what's generating right now" feed, LM Studio dev-log style

Everything above is historical — rows only appear once a request finishes. `live_requests.py`
also tracks generations *while they are streaming*: model, backend, source, tokens-so-far,
tok/s, elapsed time, request text, and response text as it arrives. The admin-only
`GET /api/metrics/live` endpoint drives the “Live developer logs” card, which polls every
second and provides an expandable request/response trace for each entry.

The in-app chat worker reports directly. Lemonade and FastFlowLM LAN traffic—including
OpenCode, Continue, and `market_analysis`—is reported by their inline proxy shims through
localhost-only `/api/internal/live/*` endpoints. Captures exclude authorization headers and
full tool schemas, are capped at 4,000 characters per request and response, and are visible
only to administrators.

Active streaming state stays in memory. Finished traces are persisted in the local
`developer_traces` PostgreSQL table, survive service restarts, and are automatically deleted
after two days (172,800 seconds). A 5,000-entry ceiling provides an additional disk bound.

Verified against the real backends: a FastFlowLM `flm/qwen3.6-moe:35b-a3b` generation showed up
immediately as `status: streaming`, `elapsed_s` ticking every poll, then flipped to `status: done`
with the correct final token count (192) and computed tok/s (3.1) matching the request's actual
62s wall-clock duration; a Lemonade NPU request that failed mid-stream (unrelated pre-existing NPU
contention with FastFlowLM's own loaded model) correctly surfaced as `status: error` instead of
hanging in the feed forever.

## Bigger context ceiling + per-client traffic attribution for OpenCode/Continue

Two asks bundled into one turn: OpenCode was hitting `exceed_context_size_error` mid-session on
the Ryzen AI NPU backend ("increase the token capacity and accept more data during the chat for
opencode"), and there was no way to tell OpenCode's, Continue's, and this app's own chat traffic
apart in the metrics board ("also make config to differentiate the traffic from opencode vs vscode
vs chat").

**Context ceiling**: bumped Lemonade/FastFlowLM's advertised context from 32768 to 65536
(`_CTX` in `help_data.py`, `flm.service`'s `--ctx-len 65536`, and Lemonade's own
`recipe_options.json` — via `ctx_size`, since Lemonade's `flm_args` allowlist explicitly rejects a
raw `--ctx-len` flag passed that way). Chosen as "a modest bump" over AskUserQuestion rather than
maxing out the NPU's real ceiling, since headroom has to be kept in sync by hand in three places
and an oversized advertised value gets the request truncated server-side instead of failing
clearly. Must stay in sync across all three or OpenCode either under-uses real headroom (advertised
too low) or overruns the server's real limit (advertised too high) — noted directly in
`help_data.py`'s own comment for whoever changes this next.

**Per-client traffic attribution**: the Caddyfile previously had one shared bearer token per
backend port. Restructured into a `(client_auth)` snippet defining three *named* Caddy matchers —
`@opencode`, `@vscode`, `@lan` — each keyed to its own distinct token, imported into every backend
port block (`route { import client_auth; reverse_proxy @opencode ...; reverse_proxy @vscode ...;
reverse_proxy @lan ...; respond 401 }`). `help_data.parse_caddyfile()` now returns
`{tokens: {label: token}, ports: {...}}` instead of a single first-match token, and both
`build_continue_yaml()` and `build_opencode_config()` embed the client-specific token in each
tool's generated config.

**A real detour discovered along the way**: the token alone couldn't attribute LAN metrics rows to
a client after the fact, because **Caddy redacts the `Authorization` header from its own JSON
access log by default** — confirmed empirically with isolated test Caddy instances (a throwaway
admin API on `127.0.0.1:2999` to avoid touching the production one) that even `header_up`-injected
values never show up in the log, since it captures the request pre-mutation. The fix was a second,
non-secret signal sent alongside the token: an
`X-Hearth-Client: opencode`/`vscode_continue` header, added
to both generated configs, which Caddy — confirmed, also empirically — logs verbatim since it
doesn't recognize it as sensitive. `caddy_metrics.py._source_from_headers()` now checks this header
first and only falls back to the old User-Agent sniffing (`_source_from_ua()`) when it's absent
(e.g. someone's own `curl` testing the `lan` token directly).

### Current `X-Hearth-Client` convention

Every OpenAI-compatible application should send a stable tracking label:

```http
X-Hearth-Client: market_analysis
```

Accepted values match `[a-z0-9][a-z0-9_-]{0,47}`: lowercase letters, numbers,
underscores, and hyphens, with a maximum length of 48 characters. Missing or invalid
values are grouped as `lan`. This is an observability label, not authentication; the bearer
token remains required. Generated configs use `opencode` and `vscode_continue`. Give every
additional application a distinct name so Metrics filters and developer traces remain clear.

```bash
curl -kN https://127.0.0.1:8444/v1/chat/completions \
  -H "Authorization: Bearer <token>" \
  -H "X-Hearth-Client: market_analysis" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.6-moe-35b-a3b","messages":[{"role":"user","content":"Analyze the market"}],"max_tokens":256,"stream":true}'
```

**Operational gotcha hit twice while testing this**: `sudo caddy stop` with no arguments targets
the *default* admin API (`127.0.0.1:2019`) — the same one the real production Caddy instance
listens on, not just whatever throwaway instance you meant to stop. Production got killed
accidentally both times during this investigation (caught within ~40 minutes via a failed
verification curl, restarted immediately, no config lost) — worth remembering that stopping an
isolated test instance means giving it its own `--config`/admin-address, never the bare command.

## Practice tests & quizzes, in the chat window

A practice-test/quiz platform that lives **inside the chat**, driven by keywords rather than a
separate page — e.g. "start DMV NC tests", "resume DMV NC tests now". `static/quiz_chat.js`
(`window.Quiz`) is a generalized quiz engine that renders questions, options, scoring, and
reasoning as chat content, and persists Q&A into the normal chat history so a test survives a
refresh or a switch between conversations (it auto-restores on load, retiring stale/deleted
sessions gracefully).

- **No repeated questions** — a deck marks its whole question pool as seen at draw time and
  validates across tests so the next test never repeats a question already asked.
- **Content** — ready-made banks plus more DMV states live in `quiz_content.py`; custom decks
  live in `quiz_store.py`. Use cases beyond DMV: **AI-generated quizzes on any topic** (generated
  by the currently-loaded model), more ready-made banks, and a **custom quiz builder**.
- **Quiz creation is admin-only** — the create/delete quiz endpoints are gated by
  `admin_required`; regular users can take any quiz but not author or delete them. There's an
  admin **Quizzes** page (`/quizzes`, `templates/quizzes.html`) for managing decks.
- The old separate **DMV** tab was removed once this moved into chat.

## Models page (redesigned from a popup, )

Model management moved from a modal popup to a full page (`/models`, `templates/models.html` +
`static/models.js`): per-backend grouped cards, summary chips, loaded-model pills, inline search,
load options, and toasts — the same load/unload/download/delete actions as before, just on a
roomier, more legible surface. The chat header's old "Models" button is now a link to it.

## Retiring LM Studio's chat models

With Ryzen AI NPU inference (Lemonade) covering day-to-day chat and cross-chat memory migrated off
LM Studio's embedding model (previous section), LM Studio's ~305GB of downloaded GGUF chat models
were no longer earning their disk space and were deleted:

```
18G   gemma-4-31B-it-QAT-GGUF
5.9G  gemma-4-E4B-it-GGUF
17G   GLM-4.7-Flash-GGUF
6.1G  Qwen3.5-9B-GGUF
17G   Qwen3.6-27B-GGUF
21G   Qwen3.6-35B-A3B-GGUF
18G   Qwen3-Coder-30B-A3B-Instruct-GGUF
202G  GLM-5.2-GGUF   (unsloth)
```

Deleted by `rm -rf` directly on `~/.lmstudio/models/{lmstudio-community,unsloth}/*` — `lms` (LM
Studio's CLI) has no delete/rm/uninstall subcommand at all (confirmed: only `load`/`unload`/`ls`/
`ps`/`get`/`import` exist), which is also why `lmstudio_client.delete_model()` deliberately raises
rather than guessing at file paths (see that function's docstring). Freed ~305GB (`df` before/after:
533G → 836G free). Verified live afterward: `lms ls` now reports just the one bundled 84MB
embedding model, `/api/models` still returns cleanly with an empty LM Studio chat-model list plus
Lemonade's models, and the app doesn't error anywhere on LM Studio having zero chat models.

**Scope of the retirement, deliberately limited**: only the model *files* were removed. The LM
Studio desktop app itself, its autostart entry, and this webui's entire LM Studio backend
(`lmstudio_client.py`, Settings panel fields, Models panel entries, `/api/chat` dispatch, Continue/
OpenCode config generator) were all left in place untouched — reloading a model through the LM
Studio app (`lms get <model>`) brings that backend right back with no code changes needed. Not
touched either: the 84MB bundled `nomic-embed-text-v1.5` file under LM Studio's own
`.internal/bundled-models/` — it ships as part of the LM Studio installation itself, isn't one of
the "downloaded models" this cleanup was about, and Continue's separate `@codebase` RAG feature
(previous section) still depends on it being there.

## Debugging case study: "make Lemonade use full throttle"

Real user report: low CPU usage while coding through Continue against the Ryzen AI NPU backend,
read as the NPU/system somehow sitting idle, wanting faster response time/throughput. Investigated
end to end rather than guessing at a fix:

1. **Low CPU usage is expected, not a symptom.** NPU inference offloads the actual compute to the
   XDNA2 chip, not the CPU — CPU staying quiet while the NPU works is the whole point of using this
   backend, not evidence it's underutilized. Confirmed this isn't a red herring before looking
   anywhere else.
2. **Checked whether the NPU was in a low-power mode.** `flm` (FastFlowLM's own NPU inference
   binary, found at `/var/lib/lemonade/.cache/lemonade/bin/flm/npu/flm-real`) exposes
   `--pmode {powersaver,balanced,performance,turbo}`, defaulting to `performance` — confirmed via
   its own `--help` and the live process command line (`ps aux`showed no `--pmode` flag at all,
   meaning it was already on that default). Forced `turbo` anyway via
   `lemonade config set flm.args="--pmode turbo"`, reloaded the model, re-benchmarked: **zero
   measurable difference** in prefill or decode speed. Also tried `--prefill-chunk-len 2048`:
   also zero difference. Reverted both — no benefit, only added config drift and (for turbo)
   possible extra power draw on a laptop-class chip.
3. **Ruled out the NPU itself being throttled**, rather than just this one model being slow, by
   benchmarking a small model (`QwenFLM06B`, 0.6B) on the exact same NPU/driver/pmode: **568 tok/s
   prefill, 62 tok/s decode**. That's full-speed hardware — proof the chip wasn't power-limited or
   globally throttled.
4. **The real number for the daily-driver model**, `qwen3.6-moe-35b-a3b`, at a realistic ~470-token
   coding prompt: **~50 tok/s prefill, ~9.6 tok/s decode**. This is the actual hardware/kernel-bound
   ceiling for a 35B-parameter MoE model on FastFlowLM's current NPU kernels — not a bug, not a
   missed setting, just what this model costs on this NPU today.

**Fix**: not a config fix — there wasn't a misconfiguration to find. Downloaded a second NPU model,
`qwen3.5:9b` (dense, registered as `qwen3.5-9b-npu`), expecting it to be the "fast" option, and
benchmarked it against the same prompt: **187-191 tok/s prefill (much faster start) but only ~5.7
tok/s decode (slower than the 35B MoE model)**. Counterintuitive but explainable: the MoE model
only activates ~3B of its 35B params per token, cheaper per-token than the dense 9B model computing
all 9B params every time. Math on the crossover: at 200 completion tokens the 35B finishes in ~30s
total vs the 9B's ~38s; the 9B only wins under ~90 completion tokens. Since real coding output
(functions, refactors) is usually well over 90 tokens, **the 35B MoE model is the objectively
faster real choice for coding**, not the smaller model intuition would suggest — added the 9B
model to the Help panel's Continue/OpenCode configs labeled accurately for what it's actually
good at (short/quick replies), not blanket-labeled "fast."

**Bigger picture, stated plainly to avoid over-promising**: there was no throttle to fix. The
system was already running at full, correct throughput for each model tested; the fix here was
picking the right model for the job (and correcting a wrong assumption about which one that was),
not unlocking hidden performance.

## Debugging case study: "model loading and unloading is not working"

Real user report, vague on purpose since the symptom was intermittent — some models loaded fine,
others didn't, no pattern given. Root-caused rather than assumed:

1. `journalctl --user -u ai-webui` showed real `502`s on `POST /api/models/load` and
   `POST /api/models/delete` that morning — not a total outage (a `POST /api/chat` right after
   succeeded fine, proving Lemonade itself was up and serving).
2. Loading the actual daily-driver model (`qwen3.6-moe-35b-a3b`) directly against Lemonade worked
   without issue — ruled out a general load/unload regression in `lemonade_client.py` or the
   `/api/models/*` routes.
3. `lemonade_client.list_models()` showed three entries that were never touched by this app this
   session: `Qwen3-Coder-Next`, `qwen3-coder-30B`, `Qwen3-30B-A3B-GGUF` — clearly an attempt to add
   a "Qwen3 Coder" NPU model via the Models panel's Ryzen AI download option, using a guessed
   checkpoint tag. Reproduced directly against `POST /v1/load`: all three fail with
   `FLM pull failed with exit code: 1` — confirmed via `flm list` that FastFlowLM's real supported
   catalog has no "coder" variant of Qwen3 at all, so no tag resembling this could ever have worked.
4. Tried the obvious next step — delete the three broken entries so they'd stop cluttering the
   Models panel. `POST /v1/delete` (and the `lemonade delete` CLI, same code path) failed too:
   `FLM remove failed for <model> (exit code 1)`, because there was never anything on disk for it
   to remove in the first place.

**This is an upstream Lemonade Server bug, not something in this app's code**: confirmed directly
by registering a deliberately fake checkpoint (`totally-fake-checkpoint-xyz`) via `POST /v1/pull`
— the pull stream correctly reports `event: error`, exactly what `lemonade_client.search_and_download()`
already surfaces to this app's UI as a failed download. But Lemonade still writes the model's
metadata into its own registry (`/var/lib/lemonade/.cache/lemonade/user_models.json`) *before*
validating the pull succeeds, and never rolls that entry back on failure — so a failed custom-model
download leaves a permanently stuck phantom entry behind regardless of how cleanly this app handled
the error at the time. There's no API-level way to clean this up (delete goes through the same
broken `flm remove` path), so a failed Ryzen AI download for a nonexistent checkpoint is a one-way
trip to a stuck Models panel entry until someone edits that file by hand.

**Fix applied**: backed up `user_models.json`, stopped `lemond` (the system-level service —
note this is a *different* unit than any user-level `lemond.service`, which was checked first and
turned out to be an unrelated leftover, inactive and not what's actually serving requests; the real
one is `/usr/lib/systemd/system/lemond.service`, running as the `lemonade` system user), removed
the three dead keys directly from the JSON, fixed ownership back to `lemonade:lemonade`, restarted
the service, and reloaded the daily-driver model. Verified live afterward: `list_models()` shows
only real, working entries; load and unload both confirmed working cleanly against the actual
model in use.

**If this happens again**: only use checkpoint tags that actually appear in FastFlowLM's own
`flm list` output (or this Help panel's documented tags) when adding a custom Ryzen AI model — a
guessed or typo'd tag will "succeed" at the registration step and only fail later at load time,
by which point it's already stuck. There's no in-app fix for the stuck entry itself; it requires
the same manual `user_models.json` edit described above.

## Memory retrieval ranking

Repeated questions can crowd relevant facts out of a small retrieval window.
Near-duplicate filtering and increasing the retrieval window from four to eight
improve recall while preserving the minimum similarity threshold.

## Embedding model reload race

Reloading an embedding model for every message can race concurrent chat requests.
Check whether the model is already loaded before calling load_model().

## Debugging case study: embedding model showing up in the chat dropdown

Real user report: the `nomic-embed-text-v1-GGUF` embedding model (RAG-only, never a valid chat
target) was showing up in the chat window's model picker, confusing users who'd pick it and get a
broken response. The frontend already had a filter meant to prevent exactly this
(`renderModelSelect()` in `static/app.js` excludes anything with `type === "embeddings"`), so this
wasn't a missing feature — it was that filter silently failing for one specific backend.

Root cause: `lemonade_client.list_models()`'s "safety net" loop (for models that are loaded but
missing from Lemonade's `show_all` catalog) hardcoded `"type": "llm"` on every entry it added,
regardless of what kind of model it actually was. The embedding model always takes this exact
path — it's `recipe: "llamacpp"`, never `"flm"`, so it never appears in the function's primary
catalog loop — meaning it was *always* mislabeled `"llm"` whenever it was loaded, defeating the
frontend's own type check every time.

**Fix**: that loop now reads each entry's real `type` from Lemonade's own `/v1/health` response
(which correctly reports `"embedding"`, singular) and maps it to this app's `"embeddings"`
(plural) convention, matching how LM Studio's API already labels its own embedding model.
Also broadened `/api/models`'s non-admin server-side filter from a hardcoded LM-Studio-specific
id check to a generic `type != "embeddings"` check, so it correctly excludes whichever backend
happens to be running the embedding model rather than only ever knowing about LM Studio's.
Verified live: `lemonade_client.list_models()` now reports `"type": "embeddings"` for
`nomic-embed-text-v1-GGUF` while it's loaded. Nothing about the model's actual availability
changed — it's still loaded and still serving cross-chat memory the same as before; only its
visibility in the chat picker was ever the target here. Admins still see it in the Models panel
(a different UI surface, for load/unload management), same as before.

## Debugging case study: response stuck repeating itself ("that's wrong, I need to check the
date again")

Real user report: a response for a local weather query got stuck in a loop, repeating the same
"Weather in Exampleville, ZZ... Weather.com" line over and over, with the model visibly
second-guessing the date mid-generation. Two separate, real bugs, not one:

1. **Root cause of *this* incident**: `looks_like_weather_query()` correctly caught the query,
   but `_geocode()` (via Open-Meteo's free geocoding API, added for the weather/world-time
   features) failed silently for "exampleville zz" — confirmed live its matcher is far pickier than
   expected: "Exampleville ZZ", "Austin TX", "Miami FL", "Portland OR", even the full state name
   "Portland Oregon" all return *zero* results; only the bare city name works. Since this is the
   single most common way people actually phrase a US location, the weather route was silently
   broken for nearly every real query, falling through to the general search path instead —
   producing generic search-engine snippets in place of real weather data. That mismatch between
   what was asked and what was actually in the injected context is the most likely trigger for
   the model's confusion and the repetition spiral that followed.
2. **Separately, a missing safety net**: neither backend was ever sent any repetition guard
   (`frequency_penalty`/`presence_penalty`) at all — confirmed by reading both `chat_stream()`
   implementations directly. Repetition-loop degeneration is a well-known failure mode for local/
   quantized models specifically, independent of whatever triggers it, which is why most local-LLM
   UIs default a mild penalty on. This app never had one.

**Fixes**:
1. `_geocode()` now retries with the trailing state token stripped (2-letter code or full state
   name) when the full phrase returns nothing, before giving up — verified live this alone fixes
   every one of the cases above, shared by both `fetch_weather()` and `fetch_world_time()` (same
   underlying bug, same geocoder, same fix). `a local weather query` now returns real current
   conditions (93.6°F, mainly clear, 43% humidity) instead of falling through.
2. `frequency_penalty` now defaults to a mild `0.3` in both `lemonade_client.chat_stream()` and
   `lmstudio_client.chat_stream()` — confirmed live the param is accepted without error by both
   the llamacpp-recipe and flm-recipe (FastFlowLM/NPU) paths. A general safety net, not a fix for
   this specific incident's root cause — pass `frequency_penalty=None` at a call site to disable
   it if it ever works against a particular model's own output in a way that matters more.

Verified end-to-end, not just each piece separately: replayed the exact original scenario
(injected weather context built the same way `/api/chat` builds it, fed into a real completion
against the live model) and got a clean, correctly-formatted weather answer with no repetition.

### Follow-up: the same user hit a second, different failure on a "storm radar" phrasing

After the Playwright page-scraping work above shipped, a real query — `"check weather radar to
see if the strom [sic] or clouds moving to exampleville zz"` — got a flat "I don't have access to
Playwright or any web browsing capabilities" from the model. Traced via `chat_history.db` (same
technique as the image-hallucination case study above: find the actual stored user/assistant turns,
not just trust the pasted response) to two compounding bugs, both upstream of the model — it never
had a chance, since no context was ever injected:

1. **`extract_weather_location()` returned garbage on natural phrasing.** Its filler-subtraction
   fallback only excluded a short, rigid list of words ("what's", "in", "at", "for", ...) — a real
   conversational sentence like this one left almost the entire query standing as the "location"
   (`"check radar to see if strom or clouds moving to exampleville zz"`), which `_geocode()` then
   silently failed to resolve (not a place name), so the weather branch produced nothing and fell
   through with no result at all — worse than the original `_geocode()` bug above, because this
   time not even the general search fallback got a clean query. Rewritten with three ordered
   strategies instead of one: (1) a US state abbreviation/name anchor — least ambiguous signal,
   checked first, catches the real query correctly (`"exampleville zz"`); (2) the *last* "in/at/for/
   near/around/to \<place\>" in the sentence rather than the first (a verbose query often has
   several, and only the rightmost one introduces the actual place), with trailing filler trimmed;
   (3) the old filler-subtraction approach as a last resort, but now capped at ≤3 surviving words —
   past that it's more likely leftover noise than a real place name, and `None` (falling through
   to general search) is a better outcome than a bad guess.
   - **A second bug found while building strategy (1), before it ever shipped**: several US state
     abbreviations collide with ordinary English words — `IN`/"in", `OR`/"or", `HI`/"hi", `OK`/"ok",
     `ME`/"me", `PA`/"pa", `MA`/"ma", and others. Caught directly by a regression test: `"the
     weather in New York"` matched "IN" (Indiana) on the word "in" and returned `"weather in"`
     before ever reaching the real "New York" later in the sentence, and the storm/radar query's own
     "the strom **or** clouds" matched "OR" (Oregon). Fixed by only trusting the ambiguous
     abbreviations when the original token was written upper-case (how real state abbreviations are
     almost always typed — "Exampleville ZZ", not "exampleville zz-the-conjunction"), while unambiguous
     abbreviations (`nc`, `tx`, `fl`, ...) still match case-insensitively as before. A 12-case
     regression battery (state-anchor, last-preposition, filler-cap, and the ambiguous-abbreviation
     collisions specifically) passes cleanly after the fix.
2. **Even with a working location, "radar" has no real answer in this app's data sources.**
   Open-Meteo (the free API `fetch_weather()` already uses) has no radar-imagery product — there's
   no live map to hand back. But the actual question behind "is a storm moving toward Exampleville" is
   answerable from data Open-Meteo *does* have: hour-by-hour precipitation probability/amount,
   which `fetch_weather()` was requesting the current-precipitation field for but never using, and
   never requested at all for the near-term hourly forecast. Added an `hourly` request
   (`precipitation_probability,precipitation,weather_code`, next 12 hours) and surfaced only the
   hours with a meaningful chance (≥30%) so real signal doesn't get buried under a wall of "0%"
   lines. The trailing instruction was also rewritten to stop the model from treating "no radar
   imagery" as "no data at all" — it's now told explicitly to name the one real gap (no visual map,
   point to radar.weather.gov) while still answering the timing/chance/current-conditions question
   fully from the real data it does have, rather than deflecting the whole question.

Verified end-to-end through a real chat completion against the exact failing query: the model
correctly said it couldn't show a live radar *map*, pointed to radar.weather.gov for that
specifically, and then still gave a detailed, accurate, data-backed answer to the actual
question — current conditions, hour-by-hour rain timing for the next 12 hours ("heavy rain starting
around 06:00-07:00, 64-70% chance"), and the multi-day outlook. Exactly the "acknowledge the one
real limitation, still answer the rest" behavior the fix was meant to produce, not the blanket
refusal from before.

## MTP (multi-token prediction) models — verified working

Checked on request whether Lemonade actually exercises MTP-capable models' speculative-decoding
head, rather than just loading them like any other GGUF and leaving the extra capability unused.
It does, and Lemonade handles this entirely on its own — nothing in this app's own code (`load_model()`
just sends `{"model_name", "ctx_size"}`, no spec-decoding flags of any kind) requests or configures
it. Confirmed via `Jackrong/Qwopus3.6-35B-A3B-Coder-MTP-GGUF` (downloaded through the Download
Models page — see that section above), by reading Lemonade's own systemd journal around model load
and real generation, not just checking that a flag was set:

- **Auto-detected at load time**: `"Model uses MTP, adding draft decoding defaults"`, followed by
  `"creating MTP draft context against the target model"` and `common_speculative_impl_draft_mtp:
  adding speculative implementation 'draft-mtp'` (`n_max=3, n_min=0, p_min=0.75`) — the actual
  running `llama-server` process confirmed this with `--spec-type draft-mtp --spec-draft-n-max 3
  --spec-draft-p-min 0.75` on its command line, flags this app never sent itself.
- **A benign red herring along the way**: a `"speculative decoding not supported by this context"`
  warning appears once during startup — but it's logged immediately after llama-server's own
  internal warm-up pass (`"warming up the model with an empty run"`), a separate throwaway context
  unrelated to real requests. Confirmed harmless by checking whether real generations that
  followed actually used speculative decoding — they did, decisively (next point).
- **Genuinely active during real generation, confirmed across multiple independent requests, not
  a one-off**: `slot print_timing` logs a `draft acceptance` stat per request —
  first request: `0.90244 (74 accepted / 82 generated), mean acceptance length = 2.76`; a second,
  unrelated request: `0.94488 (120 accepted / 127 generated), mean acceptance length = 3.03`
  (essentially hitting the configured ceiling of 3 tokens per draft round). A >90% acceptance rate
  with mean length near the max is a strong result — most generated tokens are coming from the
  cheap MTP draft path, not a full forward pass each time.

No fix needed here — this was a verification request, not a bug report, and everything checked
out working as designed.

## Pre-flight model size check — refusing loads that can't possibly fit

Real incident: a GLM-5.2 quant (`UD-IQ2_XXS`, apparently downloaded through Lemonade's own native
UI rather than this app's Download page) crashed Lemonade on load, the same `vk::DeviceLostError` /
"Not enough memory for command submission" signature as the Qwopus GPU crash earlier the same day —
but investigating turned up a completely different, much simpler root cause. `du -sh` on the actual
file: **211GB**, on a machine with **43.7GB** of total RAM. Not a driver quirk or a memory-pressure
edge case — categorically impossible to load on any backend (CPU, GPU, or NPU), the same way no
amount of retrying or rebooting was ever going to fix the Qwopus crash being framed as "the same
kind of problem" would have been the wrong diagnosis here. Lemonade doesn't check file size against
available memory before attempting a load, so it went straight through to `llama-server` and crashed
there, exactly like a smaller-but-still-too-big model would.

**`lemonade_client.check_safe_to_load(model_id)`** is a new pre-flight check, called from
`/api/models/load` in `app.py` *before* `lemonade.load_model()` is ever invoked — refuses the
request outright (HTTP 422, clear error message with the real GB numbers) rather than letting a
doomed load reach Lemonade at all. Looks up the checkpoint's real file size via Hugging Face's repo
file-tree API (the same endpoint `_pick_gguf_file` already uses for the Download page) and compares
it against `psutil.virtual_memory().total` — refuses only when the model exceeds 80% of total system
RAM, a threshold chosen to have real margin above "technically fits" (OS, this app, and Lemonade
itself all need headroom too) without being so conservative it second-guesses legitimately large
models this machine actually can run.

**A second bug found while building the size lookup, before it ever worked correctly**: the first
version returned "size unknown" for the actual GLM-5.2 checkpoint and silently let it through — the
opposite of the intended fix. Traced to how Lemonade stores this particular checkpoint:
`"antirez/glm-5.2-gguf:UD-IQ2_XXS"` — the part after the colon is a bare *quant token*, not the real
filename (`GLM-5.2-UD-IQ2_XXS_RoutedIQ2XXS_blk78Q2K.gguf`). Lemonade's own logs call this resolution
path "quant-token fallback": it substring-matches the token against the repo's actual files rather
than requiring an exact name. The size lookup didn't replicate that and only ever tried an exact
match, so it found nothing for this exact case — the one that mattered most. Fixed by mirroring the
same fallback: try an exact filename match first, then fall back to a case-insensitive substring
match, summed across every matching file (not just the first) so a model split across multiple part
files that a single quant token could match more than once is still sized correctly, not
undercounted.

Deliberately biased toward *never* blocking a load it isn't sure about: any lookup failure (network
error, checkpoint isn't HF-hosted, no matching file found) returns "size unknown" from
`check_safe_to_load`, which is treated as safe-to-proceed, not refused. A false negative here just
means Lemonade's own crash-on-load happens as before (the pre-existing behavior, no worse than
before this check existed); a false positive would incorrectly block a legitimate load, which is the
direction this needed to be careful not to fail in. flm/NPU checkpoints (bare tags like
`"qwen3.6-moe:35b-a3b"`, no HF-repo shape at all) always come back "size unknown" and are never
blocked by this check for that reason — not a gap in practice, since FastFlowLM's own curated model
catalog tops out at 24.3GB (`qwen3.6-moe:35b-a3b`, see the MTP section above), nowhere near this
machine's RAM ceiling.

Verified live end-to-end: `check_safe_to_load('glm-5.2-gguf-UD-IQ2_XXS')` now correctly refuses
("this model is 211GB, but this machine only has 46.9GB of RAM total..."), while every other
currently-known model (the Qwopus MTP model, the NPU MoE model, the embedding model, an unloaded
30B GGUF) still returns "safe" — confirming this doesn't just block the one reported model, but
also doesn't collaterally block the ones that are actually fine, including the ~20GB Qwopus model
whose *different* GPU-specific crash this check was explicitly not trying to solve (that one fits
comfortably within the RAM-based threshold here; its instability is a Vulkan driver issue, not a
"doesn't fit in memory" one — see the MTP-vs-NPU benchmark section for that separate, unresolved
problem).

**One caveat worth being upfront about**: this only guards loads that go through *this app's*
`/api/models/load` endpoint. If a model is loaded directly through Lemonade's own native UI (which
this incident's GLM-5.2 download most likely was, based on the download path in the logs), this
check never runs — it's not something this app can intercept from the outside. Nothing to be done
about that from here; worth knowing if a similarly-oversized model gets loaded that way again.

## Chat UI polish: a live "thinking" indicator, and real markdown headings

**Waiting animation.** Between hitting Send and the first token actually arriving, the empty
assistant bubble used to just sit blank — anywhere from instant to tens of seconds on a cold
NPU/model load, with nothing on screen to say it was even trying. `buildWaitingIndicator()` in
`app.js` fills that gap: two counter-rotating rings around a pulsing accent-colored core, plus a
label that cycles every 4s through "Thinking… → Warming up the model… → Still working on it… →
Almost there…" so a genuinely slow cold-start reads as progress, not a freeze. `runChatTurn()`
calls `clearWaitingIndicator()` the instant the first `reasoning_content` or `content` delta
actually streams in, stopping the label timer and removing it — the bubble takes over from there
exactly as before. Pure CSS animation (`.waiting-indicator`/`.wi-*` in `style.css`, reusing the
existing `--accent`/`--accent-dim` theme variables), verified by rendering the real markup and
diffing two screenshots a beat apart to confirm the rings had actually rotated and the dots had
actually blinked, not just that the markup was present.

**Markdown headings.** Found via a real chat reply that started with `### 5. Secure Myself and
Gather Evidence` — it rendered as literal `###` characters, hashes and all. The built-in
markdown-lite parser (`renderProse()` in `app.js`) had fenced code blocks, GFM tables, and inline
bold/italic/code, but had never had ATX-style heading support at all; any `#`/`##`/`###` line just
fell through to the plain-paragraph path. Fixed with a heading check
(`/^(#{1,6})\s+(.+?)\s*#*\s*$/`) ahead of the paragraph-buffering loop, emitting a real `<h1>`–
`<h6>` (with `renderInline()` still applied, so bold/code inside a heading keeps working) instead
of literal hash text. New `.msg-heading` CSS scales each level to fit inside a chat bubble rather
than default browser heading sizes — H1/H2 get an underline, H3 uses the accent color, H4–H6 are
small, dimmed, and uppercase. Applies retroactively to old saved messages too, since the raw text
was always stored correctly and only the renderer was missing the feature — no data migration
needed, it just renders correctly the next time that bubble is drawn. Verified by rendering a
mixed H1/H2/H3 + bold/inline-code sample against the real CSS.

## OpenCode/Continue file creation failing — a cross-backend NPU memory guard

Reported as "something is missing in the Caddy reverse proxy" — it wasn't. Diagnosis (via
Caddy's access log, `journalctl -u flm.service`, and `free -h`) traced it to Lemonade and the
standalone `flm.service` (behind the tool-calling shim on Caddy's `:8447`) both being able to
load their own independent copy of the same NPU model (`qwen3.6-moe:35b-a3b`) on demand.
Loaded at once, the two exceeded this machine's 43GB of RAM and the kernel OOM-killed
`flm.service` outright — three times in 15 hours — breaking whatever OpenCode/Continue request
(often a file-creation tool call, which needs more working memory than a short reply) was
in flight. Caddy was just relaying a crashed backend's error faithfully.

**Immediate fix**: unloaded Lemonade's duplicate copy (`POST /api/v1/unload`), freeing ~24GB.

**Permanent fix, four parts**:
- `resource_guard.py` — a new backend-agnostic pre-flight check (`check_available_memory()`,
  a 6GB floor on `psutil.virtual_memory().available`) wired into all five backends' load paths
  in `app.py`. Unlike the existing per-backend size checks (`lemonade_client`/`vllm_client`'s
  `check_safe_to_load()`, which only compare a model's own file size against *total* RAM and
  can't even evaluate NPU/flm models at all — bare tags like `"qwen3.6-moe:35b-a3b"` have no
  Hugging-Face-repo shape to size), this catches "something else already loaded a lot"
  regardless of cause. Also added `kobold_client.check_safe_to_load()` (its first pre-flight
  check ever, sized from the local GGUF file already on disk — no HF lookup needed).
- `flm_tool_shim.py` hardened to evict any Lemonade-loaded NPU model before forwarding to
  FastFlowLM — closes the direction that actually broke, since it's the one thing genuinely
  inline for real FastFlowLM LAN traffic (app.py's own `_free_npu_except()` only runs for
  loads triggered through this app's own UI/API, which a direct LAN request never touches).
- **The reverse direction** — confirmed by manually reproducing the OOM live — needed a
  second shim: Lemonade auto-loads a model on demand the same way LM Studio's JIT loading
  does, and nothing intercepted *that*. New `lemonade_guard_shim.py`
  (`lemonade-guard-shim.service`, `127.0.0.1:13306`) sits between Caddy's `:8444` and
  Lemonade's raw server: for any chat request naming an NPU model, it stops `flm.service`
  first, then forwards unmodified — everything else (embeddings, Whisper, non-NPU chat
  models) passes straight through. Caddy's `:8444` now points here instead of Lemonade
  directly. `help_data.py`'s `_UPSTREAM_BACKEND` and both of `app.py`'s port maps
  (`_read_caddy_info`, `_CADDY_UPSTREAM`) were updated to recognize port 13306 as "lemonade"
  too — the same dual-port pattern already used for the FastFlowLM shim's 52625/52626 — so
  the Help panel, Settings page, and per-user token snippets stayed correct with no other
  changes needed (confirmed live via `help_data.build()`).
- Re-verified end to end after each change: a real `write`-tool-call request through Caddy's
  `:8447` came back with a correct `tool_calls` response and no crash; the reverse collision
  was deliberately re-triggered once more through the new guard shim and confirmed
  `flm.service` now stays untouched throughout Lemonade's own load.

Also opened the UDP firewall ports Caddy's HTTP/3 (`Alt-Svc: h3=...`, on by default) needs on
every LAN port, not just the one that happened to already have it: `ufw allow 8443/udp`,
`8445/udp`, `8446/udp`, `8447/udp` (IPv4 + IPv6), matching `:8444`'s and the main app's `:5443`
pre-existing UDP rules. TCP was never blocked, so this doesn't fix a hard failure — clients
that attempt HTTP/3 on those four ports were silently falling back to HTTP/2 rather than
failing to connect — but it closes an inconsistency with no reason to exist.

### Follow-up: the metrics page's live activity feed can now see OpenCode/Continue traffic through Lemonade and FastFlowLM

The Performance metrics board's live-activity feed (see that section above) was built as
webui-chat-only because LAN traffic normally has no code path that reads a streaming body —
Caddy's raw `reverse_proxy` just pipes bytes through untouched. That stopped being fully true
the moment `flm_tool_shim.py` and `lemonade_guard_shim.py` started existing: both are real
Flask processes sitting inline for every OpenCode/Continue request to their backend, and
`flm_tool_shim.py` already parsed every SSE chunk anyway (for its degeneracy check).

New `live_report.py` — a thin client the shims use to call three new internal, localhost-only
routes on the main app (`/api/internal/live/start`/`update`/`finish`, gated by
`request.remote_addr`, not `admin_required` — the shims have no session of their own) that
forward straight into the existing `live_requests.py` registry. Each shim's `tee()` now parses
every `data:` line for `content`/`tool_calls` text (throttled to one update per ~20 characters,
not per SSE chunk, since FastFlowLM in particular streams many small chunks/sec and the feed
only needs to look like it's moving) and reports start/update/finish exactly like the webui's
own chat worker does — including swapping in a real `completion_tokens` figure from a trailing
`usage` object when the backend sends one, same as the webui path.

**A real bug caught during verification**: the first version only called `finish()` at the
normal end of the generator — an upstream connection failure (confirmed live: `flm.service`
mid-restart after an eviction) raised before reaching it, leaving that request stuck showing
"streaming" in the feed forever, since `live_requests.py`'s registry has no independent
timeout for active entries. Fixed with a `try`/`finally` around both the initial `requests.post`
and the whole streaming loop in each shim — `live_requests.finish()` is idempotent (a no-op if
already removed), so this guarantees exactly-once cleanup regardless of how the request ends,
without complicating the normal-path code. Verified live: a real OpenCode-headed request
through `:8447` and a real vscode-headed request through `:8444` both appeared correctly
(`source`/`backend` populated, `streaming` → `done`, real token counts), and the orphaned entry
from before the fix was confirmed gone after restarting the registry. Finished traces are now
persisted for two days; only active stream state remains RAM-only.

LM Studio/vLLM/KoboldCPP LAN traffic still can't appear in this feed — no shim sits inline for
those, so nothing on this app's side ever sees their streaming bodies. The live card's subtitle
and `metrics.js`'s comment were updated to say so precisely, rather than the old blanket "LAN
traffic can't appear here."

## "Lemonade models are missing" — a health-check timeout was nuking the whole catalog

Reported as "I think Lemonade crashed" — it hadn't. `lemond.service` was actively serving a
real generation (`Atomic-Germ-Qwopus3.5-9B-Coder-NPU`, mid-decode) the whole time. Confirmed
live: `/v1/models` answered in under 40ms throughout, but `/v1/health` reliably blocked past
an 8s timeout while that generation was in flight — some internal lock or single-threaded
bottleneck Lemonade's own health endpoint shares with active generation, not a bug in this app's
request itself.

`lemonade_client.list_models()` wrapped *both* calls in one `try/except`, so that one slow
`/v1/health` discarded the entire catalog — all 11 models vanished from the Models page,
looking exactly like a crash rather than "busy." Fixed by splitting them into independent
`try/except` blocks: a `/v1/models` failure still means "Lemonade unreachable" (unchanged,
returns `[]`), but a `/v1/health` failure now only costs the loaded/not-loaded state — it
defaults to "not-loaded" for that poll and self-corrects on the next one, rather than hiding
every model. Verified live: all 11 models reappeared immediately (via both the raw client and
the real `/api/models` endpoint) while the generation was still running, and the busy model's
state corrected itself the moment it finished and Lemonade unloaded it on its own.

## "OpenCode stopped responding mid-task" — a context-length regression, and a settings escape hatch

Two distinct problems surfaced from one report ("opencode ended coding in the middle... no
symptoms of connecting to the models").

**A real regression, found and fixed**: `app.py`'s `/api/models/load` Lemonade branch still
had `context_length=body.get("context_length") or 32768` — a leftover fallback from before the
context ceiling was raised to 65536 everywhere else (`help_data.py`'s `_CTX`, `flm.service`'s
`--ctx-len`, `recipe_options.json`'s stored `ctx_size`). An explicit `context_length` passed to
`load_model()` overrides whatever `recipe_options.json` has stored, so *every* load through this
app's own UI/API — not just ones requesting a custom context — was silently reloading the model
at 32768 regardless of what was advertised. Confirmed live in Lemonade's own log
(`StreamingProxy: Backend returned error: 400`) at the exact moment a mid-session "run it and
fix the issues" turn (a full coding session's worth of accumulated context) went out — OpenCode
never surfaced the 400 as a visible error, so it just looked like it "didn't respond." Fixed the
fallback to 65536, then explicitly unloaded and reloaded the live model (a plain reload was a
no-op — Lemonade doesn't restart an already-loaded model's process just because
`recipe_options.json` changed) and confirmed the new process actually launched with
`--ctx-len 65536`.

**A second, separate, still-real bottleneck**: even after that fix, real OpenCode retries kept
disconnecting client-side at almost exactly 300s (`StreamingProxy: Client disconnected during
SSE stream`), immediately followed by another retry — a genuine speed ceiling, not a bug. This
NPU model measured ~5.6 tokens/sec decode; a real coding turn's prefill (re-reading a large
accumulated history) plus generation can outlast OpenCode's own ~5-minute client timeout,
and each instant retry only made it worse by queueing behind whatever the *previous* abandoned
attempt was still finishing server-side (Lemonade serves one generation at a time).

Nothing server-side can change OpenCode's client timeout or retry behavior, so the fix here is
choice, not automation: a new **Settings → "Default LAN model (OpenCode)"** dropdown
(`lan_default_model` in `settings_store`, "backend/model_id" shaped, empty = auto), populated
live from every currently-known chat model across all five backends. `help_data._pick_default()`
now checks this override first — and falls straight through to the existing auto-pick (still
NPU-first, unchanged default behavior) if the saved model isn't actually available anymore, so a
stale setting after a model gets deleted/renamed can't silently break the generated config.
This only changes the *default* baked into OpenCode's config's top-level `"model"` field — every
model is still listed and selectable there regardless. Verified live: setting the override to
Lemonade's GPU-backed `Atomic-Germ-Qwopus3.5-9B-Coder-NPU` (tool-calling-native, no NPU
exclusivity/eviction concerns) changed the generated config's default immediately; pointing it at
a nonexistent model correctly fell back to auto; clearing it restored the original NPU default.

### Follow-up: Settings moved from a header flyout to its own page

Same move Users got earlier (see "Users page" above) — Settings was still an in-page flyout
panel toggled from the chat header, the last major admin surface that hadn't made the jump to a
real page. New `GET /settings` (`templates/settings.html` + `static/settings.js`, admin-only via
the same `admin_required` decorator as the rest), reorganized into the same `console-grid`/
`console-card` layout every other console page uses instead of one long scrolling form — one
card per backend (LM Studio, Lemonade, vLLM, KoboldCPP, FastFlowLM), one for the "Default LAN
model" dropdown, one for the read-only Caddy info. All existing content, copy, and behavior
carried over unchanged (connection tests, Caddy tests, the save flow) — this was a structural
move, not a rewrite.

`static/app.js` lost the entire settings-handling block (DOM refs, `applySettings`/
`fetchSettings`, the save handler, all ten `runTest()` button wirings, `renderDefaultModelSelect`)
now that nothing on the chat page needs it; `static/settings.js` is a self-contained copy of that
logic plus its own lightweight model fetch (just for the default-model dropdown, not the full
chat-page model-management machinery) and its own toast-style `addSystemMessage` (no chat pane on
this page, same pattern `users.js` already uses). The header's Settings button changed from a
`<button>` triggering a flyout to a plain `<a href="/settings">`, matching Models/Downloads/
History/Metrics/Users/Quizzes. While touching every page's nav bar to add the new Settings tab,
also fixed a pre-existing inconsistency noticed along the way: Users/Downloads/History were
missing the Metrics tab entirely (Models/Metrics had it, the other three didn't) — all six console
pages now list the same six tabs. Verified live: real backend data loads into every card on
`/settings`, a live "Test connection" succeeds, zero browser console errors on both `/settings`
and the chat page, and the Settings tab/link is present and correctly targeted on all six
console pages.

## LAN ports also reachable over Tailscale

Follow-up to the "Connect Timeout Error" report above: server-side diagnosis (Caddy up, correct
IP, `ufw` correctly allowing all five ports from "Anywhere," zero blocked packets logged from the
Mac's address in 2 hours of kernel firewall logs) ruled out this machine as the cause — the
timeout was in the network path between the Mac and here (client isolation, a different Wi-Fi
network, a stale route — something outside this app's or even this machine's control). The Mac
was already on the same Tailscale tailnet as this machine (confirmed via `tailscale status` —
`example-client`, "active"), so exposing Caddy's LAN ports there too gives OpenCode/Continue
a second path that doesn't depend on the LAN/Wi-Fi being cooperative.

Added this machine's Tailscale IP (`<tailscale-ip>`) to all five Caddy site blocks' address lists
(`/etc/caddy/Caddyfile`) alongside the existing LAN IP/hostname — no new ufw rules needed, since
the existing rules already allow these ports from "Anywhere" (interface-unscoped), unlike the
VNC rule elsewhere in this same `ufw status` output which is deliberately scoped `on tailscale0`
only. The real fix was Caddy-side: without the Tailscale IP listed as a site address, Caddy's TLS
layer had no certificate to offer for that SNI at all, and rejected the handshake outright
(`tlsv1 alert internal error`) — confirmed via `curl -v`, this doesn't even reach the point of
returning an HTTP error, the connection dies during TLS.

**A second attempt, reverted**: also tried adding the Tailscale MagicDNS hostname
(`machine.example.ts.net`) alongside the IP, for a friendlier address. Caddy has built-in
special-case handling for `*.ts.net` domains — it tries to fetch a *real* certificate via
Tailscale's own HTTPS-certs integration instead of using this site's `tls internal` directive,
confirmed via Caddy's own log: `"getting certificate from external certificate manager"` /
`"Access denied: cert access denied"`. That "HTTPS Certificates" feature has to be enabled for
the tailnet in Tailscale's own admin console (a manual step outside this app, and outside this
session's scope) before that path works. Removed the hostname and kept just the numeric IP,
which needs nothing extra — same `tls internal` local CA already trusted from the original CA-
trust setup, confirmed via a real (non-`-k`) `curl --cacert` request and directly inspecting the
served certificate's issuer (`Caddy Local Authority`, same as every other address on these ports).

Verified live: all five ports respond correctly over the Tailscale IP (`8443`/`8444` real `200`s;
`8445`-`8447` correctly `502`/`500` when their backends are idle, exactly matching their behavior
over the LAN IP — no different or degraded); wrong-token requests still correctly get `401`; the
original LAN IP path is unaffected (no regression). No Help panel/generated-config changes were
made — this is an additional reachability path, not a replacement default; anyone hitting the
"Connect Timeout Error" symptom on the LAN can point their tool at `<tailscale-ip>` on the same
ports as a manual workaround.

### Follow-up: the Help panel now documents the Tailscale path too

The workaround above only lived in this README — added it to the in-app Help panel so it's
discoverable without reading source history. `help_data.py` gained `tailscale_ip()`, a plain
`tailscale ip -4` CLI call (3s timeout, returns `None` on any error so a machine without
Tailscale installed just doesn't show the section at all, rather than showing a broken one) —
deliberately not a new Caddy-config parse, since Tailscale reuses the exact same site blocks,
ports, and bearer tokens the LAN path already documents, just one more address per block.
`build()`'s return dict gained a `tailscale_ip` key alongside the existing `lan_ip`.

The Help panel (`templates/index.html`) gained one new collapsible section, shown only when
`help.tailscale_ip` is set, sitting right after the OpenCode config block and before
Troubleshooting: explains the same ports/tokens/CA cert apply, shows a live before/after example
substituting the Tailscale IP for the LAN IP in a real URL, calls out when it's actually useful
(the "Connect Timeout Error" symptom above), and repeats the MagicDNS-hostname caveat (numeric
IP only, `*.ts.net` needs a Tailscale-admin-console setting this app can't turn on) so nobody
re-discovers that dead end by hand. As before, the generated Continue/OpenCode configs in Step 2
still default to the LAN IP unchanged — this is purely additive documentation, swap the IP
yourself if you need the Tailscale path.

### Follow-up: Tailscale got real Settings controls, not just Help-panel text

The section above was read-only documentation — nothing an admin could actually turn on/off or
tune. Added a "Tailscale" card to the Settings page (`/settings`) with three persisted options
(new `settings_store` keys, same pattern as `lan_default_model`):

- **Show the Tailscale section on the Help page** (`tailscale_enabled`, default on) — an
  outright kill switch, for an admin who has Tailscale installed for unrelated reasons and
  doesn't want this app mentioning it at all.
- **Use the Tailscale IP (instead of the LAN IP) in the generated Continue/OpenCode configs**
  (`tailscale_prefer_configs`, default **off**) — the explicit ask when the base feature was
  built was "keep the LAN as is," so this stays opt-in; flipping it on switches the Step 2
  config blocks over to the Tailscale IP for a setup where every client only ever connects over
  Tailscale anyway. `help_data.build()` now threads a single `config_ip` (Tailscale IP if this
  is on and one's available, else the LAN IP as before) through `ca_trust_commands()`,
  `build_continue_yaml()`, and `build_opencode_config()`, so the CA-trust command's URL and the
  generated configs never disagree about which address to use.
- **Manual IP override** (`tailscale_ip_override`) — `tailscale_ip()` shells out to the
  `tailscale` CLI as this app's own process, which can fail in some service/sandboxing setups
  even when Tailscale itself is fine; this lets an admin who already knows the IP type it in
  directly instead of debugging CLI access from within the app. Wins over auto-detection when
  set, verified by setting it to a throwaway IP and confirming the Help page's config
  immediately reflected it, then clearing it and confirming auto-detection took back over.

Also added five "Test \_\_\_ via Tailscale" buttons (mirroring the existing "Test \_\_\_ via
Caddy" row) that hit each backend's Caddy port through the Tailscale IP instead of localhost —
confirms Caddy actually has a site block matching that address, the same failure mode diagnosed
in the base Tailscale work above (`tlsv1 alert internal error` when it doesn't). Backing route
(`/api/settings/test`) gained a `tailscale_*` target family alongside the existing `caddy_*`
one; `_test_caddy()` gained a `host` parameter (default `localhost`, unchanged for every
existing caller) so the same test function serves both.

Verified live end-to-end: fetched `/api/settings` and confirmed the three new fields and a live
`tailscale_detected_ip` reading; ran a real `tailscale_lemonade` test and got back "Reachable via
Caddy on <tailscale-ip>:8444, authenticated OK"; toggled `tailscale_prefer_configs` on and
confirmed the OpenCode config's `apiBase` switched to the Tailscale IP, then off again and
confirmed it switched back to the LAN IP; toggled `tailscale_enabled` off and confirmed the Help
page's `tailscale_ip` field (and therefore the whole section) disappears, then back on and
confirmed it reappears — all settings left back at their defaults (enabled, no override, prefer
off) afterward.

### Follow-up: ready-to-copy Continue/OpenCode configs for Tailscale, not just a "swap the IP" note

The Tailscale Help section so far only explained the substitution in prose — an admin had to
manually re-derive a whole config from Step 2's LAN version by hand. Added actual pre-generated
config blocks for both tools, same copy-button UX as Step 2's.

`build_continue_yaml()` and `build_opencode_config()` both gained a `network_label` parameter
(default `"LAN"`, so every existing call site is unaffected) — purely cosmetic, it only changes
the model display names and the Continue config's title (e.g. `"Ryzen AI NPU, LAN"` vs `"Ryzen
AI NPU, Tailscale"`); the actual connectivity is entirely determined by whatever IP is passed
in, unchanged. `help_data.build()` now calls each builder a second time with the Tailscale IP
and `network_label="Tailscale"` whenever one is known, exposed as new `tailscale_continue_yaml`
/ `tailscale_opencode_config` keys — generated unconditionally (regardless of the
`tailscale_prefer_configs` toggle), since a device that only ever reaches this machine over
Tailscale shouldn't need an admin to flip a global default just to get a working config for
itself. Also fixed the *existing* `continue_yaml`/`opencode_config` keys to pass
`network_label="Tailscale"` too when `tailscale_prefer_configs` is on — previously they'd have
kept saying "LAN" in the model names while actually pointing at the Tailscale IP, which the
Settings-page work above missed.

The Help panel's Tailscale section (`templates/index.html`) gained a "Tailscale configs" block
with the two new config bodies in `<pre>` tags plus copy buttons, wired up in `static/app.js`
via the same generic `wireCopyButton()` helper Step 2's blocks already use. Verified live:
`tailscale_continue_yaml`/`tailscale_opencode_config` both contain `<tailscale-ip>` and
`"Tailscale"` labels while the main `continue_yaml`/`opencode_config` still show the LAN IP;
confirmed the two new `<pre>` blocks render with real content on the actual page; confirmed the
whole block (like the rest of the section) disappears cleanly when `tailscale_enabled` is
turned off and reappears when turned back on, with no leftover `None`/empty markup either way.

### Follow-up: dropped the LAN/Tailscale config swap — both are always shown together now

Real feedback: the `tailscale_prefer_configs` toggle added just above (Settings page) had Step
2's LAN config switch over to the Tailscale IP when turned on — but some users want the LAN
config kept even with Tailscale enabled (e.g. a laptop that's usually on the same LAN and only
needs Tailscale occasionally, off-network). Swapping meant they'd lose the LAN config entirely
just for turning Tailscale on.

Removed the toggle and the swap behavior. `help_data.build()` no longer computes a shared
`config_ip`/`config_network_label` — Step 2's `continue_yaml`/`opencode_config` always use the
LAN IP unconditionally now, same as before Tailscale existed at all. The Tailscale-flavored
`tailscale_continue_yaml`/`tailscale_opencode_config` pair (added just above) already didn't
depend on that toggle, so it's unaffected — both pairs are now simply always present together
whenever a Tailscale IP is known, no setting decides which one "wins." `tailscale_enabled`
(Settings page) still exists and still does exactly one thing: show or hide the whole Tailscale
section (and therefore both its configs) on the Help page.

Also closed a real gap this surfaced: Step 1's CA-trust command fetches from this app's own
`/ca-cert` route addressed at the *LAN* IP — a device that can only reach this machine over
Tailscale (the entire reason it'd be using the Tailscale section) might not be able to reach
that LAN address to run it. Confirmed live that this app (bound to `0.0.0.0:5443`) answers
`/ca-cert` on the Tailscale IP too, so `help_data.build()` now also generates
`tailscale_ca_trust_commands` (same `ca_trust_commands()` helper, called with the Tailscale IP)
whenever one's available. The Help panel's Tailscale section shows this as its own small
"Trust the CA — Tailscale-only devices" block, with a note to skip it if the device can already
reach Step 1's LAN version fine.

Verified live: `continue_yaml`/`opencode_config` contain only the LAN IP regardless of any
Tailscale setting; `tailscale_continue_yaml`/`tailscale_opencode_config` still correctly contain
the Tailscale IP; a real `curl -sk https://<tailscale-ip>:5443/ca-cert` returned the cert
(HTTP 200, 626 bytes) confirming the new Tailscale CA-trust command actually works; Settings
page and `/api/settings` no longer reference the removed toggle anywhere.

## "OpenCode terminated due to error" — a malformed abort chunk when the NPU model degenerates

Root-caused a real report by reading three logs in parallel (`flm.service`'s raw NPU log,
`flm-tool-shim.service`'s access/error log, and the live-activity feed): a long OpenCode agentic
session on the FastFlowLM NPU model (`qwen3.6-moe:35b-a3b`) ran a single tool call (an `edit`
argument) for 3325 completion tokens at ~7.3 tok/s (~7.6 minutes) — well into that, the model's
output degenerated into meaningless ASCII word-salad (confirmed: 243 consecutive alphabetic
characters, zero vowels among them, vs. ~41% vowels in a same-length span of the real code a few
lines earlier). `flm_tool_shim.py`'s existing reactive degeneracy guard (`_looks_degenerate()`,
added  for a different, CJK-repetition failure mode) correctly caught this too — the
low-character-diversity check it already had (≤15 distinct characters in a trailing 300-char
window) fires on this word-salad pattern as well, confirmed by replaying the exact captured text
against the live function. So the guard *did* fire and sent a graceful synthetic closing chunk
(`finish_reason: "stop"`, never `"tool_calls"`) — but OpenCode still terminated with a
client-side error right after.

Found the actual gap: that synthetic `_abort_chunk()` had hardcoded `"model":
"qwen3.6-moe:35b-a3b"` (wrong for any other FastFlowLM model) and didn't set `created` at all.
The Vercel AI SDK's openai-compatible provider — what OpenCode's generated config uses — 
validates each streamed chunk against a schema; a chunk with a missing `created` timestamp
and/or a `model` field that doesn't match what was actually requested is a plausible trigger for
exactly this kind of client-side crash immediately after an otherwise well-formed abort.

**Fix:** `_abort_chunk()` now takes the request's own (already-`flm/`-prefix-stripped) `model`
value and sets a real `created = int(time.time())`, matching what a normal FastFlowLM/OpenAI
chunk looks like. Considered also tightening `_looks_degenerate()` further (e.g. a vowel-ratio
check) but decided against it — vowel-sparse text is also completely normal in real code (UUIDs,
hashes, base64 blobs, URLs), so that signal risks false-positives the existing character-
diversity/non-ASCII checks don't have; the existing guard already catches this failure mode
correctly, so the schema fix was the actual gap worth closing.

Verified live: `_abort_chunk()` now round-trips through a direct call with a real model id and
produces valid JSON with a nonzero integer `created` and the correct `model`; restarted
`flm-tool-shim.service` (confirmed no in-flight connections on :52625/:52626 first, so nothing
was interrupted) and re-verified `GET /v1/models` through Caddy's `:8447` still returns the full
catalog normally.

## NPU usage metrics — live "is the NPU busy right now" plus a historical KPI

Real gap while debugging the incident above: nothing in the app could actually say whether a
given request ran on the NPU. `lemonade_guard_shim.py` already had this logic buried in its own
eviction check (`_is_npu_model()`, querying Lemonade's `/v1/models?show_all=true` for
`recipe == "flm"`), but it wasn't exposed anywhere. Also confirmed a real trap while building
this: a Lemonade model literally named `Atomic-Germ-Qwopus3.5-9B-Coder-NPU` turned out to be
recipe `llamacpp`, not `flm` — a plain GGUF model, not actually NPU-backed despite the name.
Lemonade multiplexes several recipes (llamacpp, flm, whispercpp, kokoro, sd-cpp, vllm...) behind
one API, so "is this Lemonade request on the NPU" can only be answered from the catalog's real
`recipe` field, never guessed from a model's display name.

**Added `lemonade_client.npu_model_ids()` / `is_npu_model()`** — a 60s-TTL-cached set of the
catalog's actual flm-recipe ids (TTL, not permanent, since the catalog changes as models are
downloaded/deleted — unlike `npu_tops_max()`'s one-time hardware-spec cache right above it).

**Real-time ("streaming can show NPU being used or not")**: every place that already reports
into the live-activity feed now tags each entry with a plain `is_npu` boolean, computed once at
request-start where the context to answer it already exists — `flm_tool_shim.py` always passes
`True` (FastFlowLM is NPU-only, no lookup needed), `lemonade_guard_shim.py` reuses the
`_is_npu_model()` check it already runs for eviction, and app.py's own webui-chat path checks
`backend is fastflowlm or (backend is lemonade and lemonade.is_npu_model(model))`. Threaded
through `live_requests.start()` → `live_report.start()` → the `/api/internal/live/start` route
→ `snapshot()`'s per-row output, plus a new `live_requests.npu_busy()`. The metrics page now
shows a live "⚡ NPU busy" / "NPU idle" badge next to "Live activity" (derived client-side from
the same rows the feed already polls every second, no extra request) and an NPU badge on each
individual streaming row.

**Historical ("add metrics for NPU usage")**: `request_metrics` gained an `is_npu` column
(migrated via the same `ALTER TABLE` + `try/except OperationalError` pattern chat_store.py
already uses for its own post-launch columns) — `1` confirmed NPU, `0` confirmed not, `NULL`
unknown. webui-chat rows get the exact same computed boolean as the live feed. FastFlowLM LAN
rows (via `caddy_metrics.py`'s Caddy-access-log tailer) get `True` unconditionally — FastFlowLM
is NPU-only regardless of which model was requested, so this needs no body access. Lemonade LAN
rows stay `NULL`/unknown on purpose: `caddy_metrics.py` never sees the request body (Caddy's
access log has no model id in it), and guessing `False` there would silently undercount real NPU
usage instead of just reporting the honest "unknown" for that one slice. The metrics page's KPI
row gained an "NPU-served" count with a percentage computed against known-answer rows only (not
against total requests, for the same undercounting reason), and the Recent Requests table gained
an "NPU" column.

Verified live end-to-end: a real streaming FastFlowLM request showed `is_npu: true` in
`/api/metrics/live` while in flight; a fresh FastFlowLM LAN poll recorded `is_npu: 1` in
`request_metrics` (old pre-fix rows correctly stayed `NULL`, not retroactively changed); a real
webui-chat turn against a genuine NPU model (`QwenFLM06B`, confirmed via `is_npu_model()`)
recorded `is_npu: 1` end-to-end through the same code path used for the historical KPI.

## Speeding up OpenCode/VSCode coding responses — GPU offload + speculative decoding

Real complaint: NPU model responses feel slow for OpenCode/Continue coding sessions. Investigated
whether any "parallel token processing" feature exists to speed up NPU decoding specifically —
it doesn't. `flm-real serve --help` (FastFlowLM, the actual NPU engine) has no speculative-
decoding, batching, or parallel-slot flags at all; autoregressive decode on the NPU is inherently
sequential, and there's no lever to change that. That's a hardware+engine ceiling, not a missing
setting — confirmed by reading the actual CLI surface rather than assuming.

What genuinely *does* support this: KoboldCPP (a llama.cpp server this app already manages via
`kobold.service`) has real Vulkan GPU offload (this machine's iGPU, AMD Ryzen AI 9 HX 470 /
Radeon 890M) and real speculative decoding (`--draftmodel`, a small model proposes tokens the
big one verifies in a batched forward pass). Built this out as a genuine, benchmarked feature
rather than a token gesture:

**`kobold_client.py`** gained `_write_service_env()` support for three new global settings
(`kobold_gpu_layers`, `kobold_draft_model`, `kobold_draft_amount` — global, not per-model-id,
since KoboldCPP only ever serves one model at a time anyway) plus a `_resolve_gguf_path()`
helper to turn a configured draft model id into an actual file path. **`kobold.service`**'s
`ExecStart` now uses bash `${VAR:+...}` expansions to conditionally add `--usevulkan
--gpulayers`/`--draftmodel --draftamount --draftgpulayers` only when those settings are actually
set — an empty/unset value cleanly omits the flag rather than erroring, and the unit file itself
never needs hand-editing again.

**A real trap caught immediately**: the first pairing tried (Qwen2.5-Coder-7B target +
Qwen2.5-Coder-0.5B draft, seemingly the obvious same-family choice) logged `WARNING: Draft
model vocab of (151936) does not match base vocab of (152064)` — Qwen2.5-Coder's 7B model uses a
different tokenizer vocab size than its own 0.5B/1.5B/3B siblings, a genuine upstream
inconsistency, not a quantization issue. Speculative decoding with a mismatched vocab silently
malfunctions rather than erroring, so this had to be caught by reading the load log, not assumed
from the pairing "sounding right." Switched to Qwen3-8B + Qwen3-0.6B (confirmed matching
`vocab_size: 151936` via each model's `config.json` before downloading anything else) — loaded
cleanly with no warning, both fully offloaded (37/37 and 29/29 layers) to Vulkan.

**Measured live, isolated** (`flm.service` stopped during the test to remove NPU/RAM
contention, via `/api/extra/perf`'s real per-request stats, not a guess): plain Vulkan decode
(no draft model) — **4.33 tok/s**; with speculative decoding — **5.11 tok/s** at an 83%
draft-token-acceptance rate (`last_draft_success: 327` vs `last_draft_failed: 67`). A genuine,
correctly-functioning **~18% speedup** — but this iGPU's raw Vulkan compute ceiling for a dense
8B model lands in roughly the same range the NPU already gets for similarly-sized dense models
(a 9B NPU model measured ~5.7 tok/s in earlier testing) — not a decisive win, and reported to the
user as exactly that rather than oversold. The NPU's 35B MoE model (~9.6 tok/s, only ~3B active
params per token) remains the fastest single option available on this hardware for larger
coding turns.

**Decision (explicit ask, not assumed):** given a real if modest win, set as the new OpenCode/
Continue default: `lan_default_model` → `kobold/koboldcpp/Qwen_Qwen3-8B-Q4_K_M` (note the
double `kobold`/`koboldcpp` — the Settings dropdown's override format is
`<backend-key>/<model-id>`, and KoboldCPP's own model ids already carry a `koboldcpp/` prefix as
part of their identity, unlike Lemonade's plain ids — a real gotcha hit live: the override was
silently ignored and fell back to auto-pick until this was corrected). Settings page's KoboldCPP
card gained three new fields (GPU layers, draft model id, draft tokens per step) so this is
tunable without touching `settings_store` directly, with an inline warning about the
vocab-matching requirement so the same mistake doesn't get repeated. Verified live end-to-end:
`/api/help`'s generated `opencode_config.model` correctly resolves to the new default; Settings
page shows and persists all three new fields; the service survived an `ai-webui.service` restart
with the right model still loaded and serving.

## Running as a service

A systemd **user** service is set up at `~/.config/systemd/user/ai-webui.service` (no `sudo`
needed) so it starts on login/boot and restarts automatically if it crashes:

```bash
systemctl --user status ai-webui      # check status
systemctl --user restart ai-webui     # restart (e.g. after editing .env)
systemctl --user stop ai-webui
journalctl --user -u ai-webui -f      # tail logs
```

Enable lingering with `sudo loginctl enable-linger "$(id -un)"` to keep the user service
running without an active login, including after reboot. PostgreSQL uses the Compose
container's `unless-stopped` restart policy; Docker must also be enabled at boot.

## Notes

- The header shows live CPU and RAM usage (`/api/system-stats`, polled every 3s, via `psutil`),
  color-coded (green/yellow/red at 70%/90% thresholds). Visible to all logged-in users, not just
  admins — it's just host resource info. Hidden on mobile to save header space.
- Chat requests are proxied to LM Studio's OpenAI-compatible `/v1/chat/completions` endpoint and
  streamed back to the browser as they're generated.
- The Send button becomes a **Stop** button while waiting for/streaming a response (`AbortController`
  on the fetch). This isn't just a client-side UI stop — verified directly that it actually cancels
  generation on the backend: aborting the browser connection tears down this app's SSE generator,
  which closes the underlying connection to LM Studio, which the backend detects as a
  dropped client and cancels generation for — confirmed via `lms ps` flipping from `GENERATING` back
  to `IDLE` within ~1s of the abort, freeing that parallel slot for other requests. Whatever content
  streamed before Stop was clicked is kept (marked "stopped" in the message meta) and persisted to
  the conversation, same as a normal completed reply — the backend's `finally` block saves partial
  content the same way regardless of *why* the stream ended.
- Model load/unload go through the `lms` CLI since LM Studio's HTTP API doesn't expose that.
- Only one model may be loaded at a time (server-enforced, to avoid overloading the machine):
  loading a model automatically unloads any other currently-loaded model first. The UI confirms
  before doing this if it would unload something. Re-loading the model that's already loaded is a
  true no-op — `lms load` on an already-loaded model otherwise spins up a second parallel instance
  rather than doing nothing, which this specifically guards against.
- Conversations are persisted to PostgreSQL through `DATABASE_URL`, auto-titled from
  the first message. The sidebar's "Chats" section lists, resumes, and deletes past conversations.
- Each message shows a small timestamp under it, and assistant replies also show generation time,
  the model that produced them, and total tokens used (e.g. "Jul 19, 2:45 PM · 3.2s ·
  qwen/qwen3.6-35b-a3b · 209 tokens"). Duration is measured server-side (wall-clock around the LM
  Studio request); token counts come from LM Studio's `stream_options.include_usage` (standard
  OpenAI-API streaming usage reporting). All of it is persisted per message, so it's correct both
  live and when reopening a saved chat — including for older conversations that mixed models before
  this existed (those just show blank for the fields that predate them).
- For reasoning models (the OpenAI-API `reasoning_content` delta field, e.g. Qwen3/QwQ-style
  "thinking" models), the thinking process streams live into an expanded, auto-scrolling "Thinking"
  section above the reply as it's generated, then auto-collapses once the actual answer starts
  arriving. It's persisted too, so reopening a saved chat still has it available — collapsed by
  default, click to expand. Not sent back to the model as conversation context on later turns (only
  the final answer is), matching how the API itself treats it.
- Model management (list/load/unload, "Load options") lives in a dropdown opened via the **Models**
  button in the top header, not the sidebar — the sidebar is chat history only. The header's
  model-select dropdown is a separate, simpler control for picking which *already-loaded* model to
  chat with.
- Loading an LM Studio model defaults its GPU offload to `max` and its context length to `32768`
  (both server-side in `app.py`, unless the corresponding "Load options" field overrides them) —
  LM Studio's own auto-detect on this box's Vulkan-capable iGPU (Radeon 890M) was choosing 0 GPU
  layers (pure CPU, a measured ~70% slower), and its stock context default is a much smaller 8192
  regardless of the model's actual max, which agent-mode tools (Continue, OpenCode) routinely
  exceed with their system-prompt + tool-definition preamble (`exceed_context_size_error`).
  Reloading an *already-loaded* model to pick up a new GPU/context/TTL setting requires an explicit
  Unload first — Load silently no-ops on a model that's already loaded (to avoid `lms load`
  spinning up a second parallel instance), so changing settings on a running
  model doesn't take effect until you unload and load it again.
- Loading a model when another is already loaded unloads the old one first (see below), then loads
  the new one, retrying up to 3 times with a short backoff — LM Studio can transiently reject a load
  attempt made immediately after an unload, before it's fully released the previous model's
  resources. Without this, that race could fail the whole request after the old model was already
  unloaded, leaving nothing loaded.
- The Models panel also has a "Search & download a new model" box, backed by `lms get <query> -y`.
  This searches LM Studio's curated catalog by name (a narrower list than all of Hugging Face — an
  exact repo id like `publisher/model-name`, or a full Hugging Face URL, always works too). Progress
  streams live into a log box; `-y` auto-approves any picks so it never hangs waiting for input.
  Downloads only add the model to disk — load it afterward from the model list like any other.
  Confirms before starting, since matches can be several GB.
- Assistant replies are rendered through a small built-in markdown-lite parser: fenced code blocks
  get a monospace panel with lightweight syntax highlighting (comments/strings/numbers/keywords),
  a language label, and **Copy** / **Download** buttons (prompts for a filename, saves via a client-side
  blob — no server round-trip). GFM-style pipe tables render as real `<table>`s with a **Copy
  table** button (tab-separated, for pasting into a spreadsheet). ATX-style headings (`#` through
  `######`) render as real, bubble-scaled `<h1>`–`<h6>` elements, not literal hash characters.
  Inline `` `code` ``, **bold**, and *italic* are also supported. This renders live as the response
  streams in, and again when reopening a saved chat.
- A multi-line input toggle switch sits next to the Send button (persisted in the browser) and changes what Enter does in
  the composer: off (default) — Enter sends, Shift+Enter inserts a newline; on — Enter inserts a
  newline, Ctrl+Enter (⌘+Enter on Mac) sends. The Send button always works either way. A hint above
  the composer shows the active keybinding.
- This runs Flask's built-in server directly with a TLS context, which is fine for personal/local
  use but isn't a hardened production setup.

## LM Studio named tool choices and reasoning budgets

`lmstudio-shim.service` runs the compatibility/metrics proxy on localhost:13307.
Its startup hook (`wire_lmstudio_proxy.py`) updates Caddy's existing local LM Studio
upstreams through Caddy's local admin API, preserving the TLS listeners and bearer
matchers. The user service is enabled at login; restart it after independently
reloading Caddy's configuration to reapply routing. The original Caddyfile is not
rewritten by the hook. LM Studio itself remains on localhost:1234.

The proxy translates an OpenAI named `tool_choice` into LM Studio's `required`
choice while offering only the selected function. Unknown function names return
400 before inference. String choices and caller token limits are preserved.

Reasoning consumes the same output budget as arguments. For Qwen tool calls,
start with at least 1024 output tokens, increasing for complex requests. A
256-token test can finish in reasoning before a tool call is emitted; the proxy
does not silently raise caller limits or fabricate a tool call.

For a single-model session, set the database setting `support_models_keep_loaded` to
`false` to suspend the periodic embedding/STT/TTS warm-up. These models still load
on demand when their features are used. Set it back to `true` to restore warm-up.

### OpenCode file tools and project discovery

Generated `flm-small` agents read project instructions, README, imports, and relevant
source before editing. File edits are allowed; shell commands still ask for approval.
The installed agent can be updated by re-copying Help's OpenCode configuration and
running `opencode reload`, then starting a fresh session.

The FLM proxy follows the request's tool schemas: OpenCode 2 uses `path`, older
clients may use `filePath`, and `apply_patch` embeds paths in `patchText`. Edit
aliases such as `old_string` are normalized only when the offered schema calls
for `oldString`. Empty runtime tool calls finish as text rather than triggering
OpenCode's “tool call delta is missing id or name” error. MiniCPM retains its
native reasoning mode unless the client explicitly disables it.

A working tool connection does not guarantee coding accuracy. MiniCPM 1B passed
one read/edit/check exercise but also chose incorrect files and changes on repeated
runs. Use a stronger coding model for reliable work across a project.

Qwen3 8B uses JSON objects inside `<tool_call>` tags, matching its installed
tokenizer template; the older XML `<function=...>` instructions belong to other
FLM model families. The proxy selects this format by model and adds `/no_think`
to the latest user message when Qwen3's thinking mode is disabled, because that
runtime ignores the template keyword alone. Explicit thinking preferences remain
supported.

Run the live FLM model checks after loading the target model:

```bash
.venv/bin/python tools/test_flm_model.py --model qwen3-it:4b --suite all --output /tmp/flm-results.json
```

This checks chat, streaming, tool choices and arguments, follow-up results/errors,
parallel calls, blocked-shell termination, 24K-token code input, and context overflow.
Returned tool calls are validated without executing them. OpenCode file-editing
regressions run without model inference against an isolated PostgreSQL schema with
`.venv/bin/python tools/run_tests.py`.

### Model tests in the Models page

Each model test target explicitly unloads the previous local chat model, loads the
requested target, and checks the inventories again. Validation refuses to send
requests if the target is absent or another local chat model remains loaded.
LM Studio and Lemonade share the coding-request policy and question/file-content
normalizers. LM Studio holds only tool-call fragments for normalization; ordinary
text continues streaming as it arrives.

Tool argument parsing recovers a complete JSON object followed only by redundant
closing brackets/braces. It rejects extra JSON values, trailing text, markup and
incomplete objects. If FLM's native streaming parser emits only
`[Error parsing tool call]` and no structured call, Hearth makes at most one
non-stream recovery request with a 90-second timeout, retaining the NPU lock.
Recovered calls still pass schema and unsafe-command guards before being
converted back to SSE. Responses that already emitted any tool call are not retried.

For an unambiguous latest-user request to call one function with exact JSON
arguments, optional fields invented by the model are removed only if the function
and every supplied value already match. Unknown or required extra fields, changed
values, reordered/extended arrays, multiple calls and truncated output are not
rewritten. This prevents an unrequested `replaceAll: true` from broadening an edit.
Whole-file read instructions also distinguish full reads from explicit pagination.

`tools/test_support_models.py --after-job JOB_ID` waits for a completed chat matrix,
then unloads models between embedding, TTS/STT round-trip, SD-Turbo image, and Wan
video endpoint checks. It writes a separate JSON report; media models are not
misrepresented as chat/tool-calling targets.

Every loadable chat-model card has **Test tools** and **Reset repair**. The test panel
can queue all installed chat models, display the current check and elapsed time,
cancel after the current request, reopen recent runs, and download JSON reports.
It checks chat, streaming, tool choices, JSON argument types, parallel calls,
file-tool contracts, result/error follow-ups, large-code input, and context overflow.
OpenCode coverage adds 78 step checks (39 per response mode): a 14-step build
with planning, file discovery/search, reads, edits across files, test creation,
a simulated failing test and recovery, verification, and completion; single-choice,
multiple-choice, custom-answer, batched-question and dismissal/resume conversations;
failed-edit recovery; parallel reads; denied installation permissions; task results;
paginated reads that preserve source text without line-number prefixes; and resume
from a supplied session checkpoint after context compaction.
Schemas follow locally captured OpenCode tool requests. Each conversation carries
validated model tool-call IDs and fixture outputs into subsequent requests. A failed
step blocks dependent requests and appears as **Blocked**, never a pass. Other
workflows continue. These are scripted protocol/continuation tests with explicit
next-step instructions, not proof that a model can autonomously build arbitrary
large software. Tool results, delegated task results, permission decisions and user
answers are simulated; a successful run does not claim real tests were executed.
The suite has 113 checks for FLM (111 for other backends). Long models can take
considerably longer than the earlier single-turn suite.

Run only these scenarios from the CLI with `--suite opencode`, for example:

```bash
.venv/bin/python tools/test_flm_model.py --model qwen3.8-mtp:27b --context 8192 --suite opencode
```

The blocked-shell guard check applies to the FLM proxy. Test payloads never execute
model-generated tools, change a real project, or send messages to connected accounts.

Runs require an administrator and CSRF protection. Local models use the existing
serialized load/eviction and memory/NPU checks. Other model-management operations
are blocked until the test run finishes or is cancelled. Runs continue when the
browser closes; the latest 30 reports are stored beside the chat database in
`model-test-runs/`. A server restart interrupts an unfinished run rather than
silently restarting model loads.

Automatic repair is a bounded recipe, `strict-tools-v1`: exact tool/schema guidance
and deterministic sampling. The candidate is isolated to validation requests until
the entire suite has been rerun. It is published only if the set of failures strictly
shrinks without regressions; otherwise it is rolled back. Original attempts and
rollback markers remain in the report. Already-applied recipes are not retried
indefinitely. Model capability gaps and unsupported runtime features are reported
as unresolved, never hidden by weaker assertions or arbitrary generated code edits.
Profiles apply to Hearth backend clients and local FLM/LM Studio/Lemonade proxies;
clients talking directly to external servers need corresponding configuration.
Reset repair removes the persisted `tool_test_profile:<backend>:<model>` setting.

Validation:

```bash
.venv/bin/python tools/run_tests.py --browser
```

The browser check uses isolated simulated models and checks progress, verified
repair, HTML escaping, report download, reload recovery, reset, and cancellation.


### Qwen3.8 FLM context and output budgets

Qwen3.8-27B on the installed FLM v1.0.8 NPU engine uses an 8,192-token combined
context and now permits up to 4,096 output tokens (previously 2,048). The proxy,
Hearth client, and generated OpenCode `limit.output` agree on this budget. Explicit
smaller requests remain smaller. Long prompts reduce the output budget to fit,
including the existing 256-token safety reserve; conversation turns are retained.
Explicit replies shorter than 256 tokens reserve only their requested reply length,
so those requests can use more of the input window without reducing safety padding.
Regenerate your OpenCode config from Help to pick up the new advertised output.

Larger input windows cannot currently be enabled just by changing `--ctx-len`.
Direct 12,288-, 16,256-, and 16,384-token trials failed during model loading:
`attn_layout: max_len 16512 ... max_len is at most 16384 (MAX_L_LIMIT)`.
The engine's padded allocation exceeds its attention-layout limit. The working
8K setting was restored. The model's native context metadata does not override
this NPU restriction; a compatible engine/kernel update is needed for larger inputs.


### OpenCode question compatibility and validation diagnostics

The FLM and Lemonade proxies normalize OpenCode `question` calls: an omitted
optional `multiple` becomes `false`, and options labelled `(Recommended)` move
first without changing labels or descriptions. Explicit multiple-selection
requests stay unchanged. Lemonade applies this to non-streaming completions and
after assembling streamed argument fragments, preserving call IDs and usage.
This prevents valid single-choice questions from blocking the rest of validation.

The large-code sentinel check now requests 1,024 output tokens and explicitly
disables thinking in both supported request forms; the earlier 128-token budget
could be consumed by a reasoning pass before the answer. Expected answers and
workflow assertions remain unchanged. Reports retain usage and finish reasons
for failed assertions, with a useful budget explanation for truncated replies.

Streaming test responses are decoded as UTF-8 and split only on SSE newline
boundaries. This avoids false JSON failures for Unicode reasoning text. The
large-code check permits up to 900 seconds for CPU prefill; other checks retain
their 300-second limit. Lemonade tool requests set the template thinking option
as well as the top-level option, respect explicit preferences, and restrict the
Qwen repetition penalty to Qwen models so exact source copying on other models
is not penalized. Instructions preserve literal file content, choice ordering,
and explicit requests to stop after dismissed questions.

Lemonade also preserves an explicit user-supplied `write`/`write_to_file` JSON
body when the model changes only empty lines or the final newline. This requires
an unambiguous latest user instruction to call that tool with exactly the supplied
arguments, a declared tool schema, the same file path and argument keys, and a
single completed tool call. Content is restored from that user instruction, never
from test expectations or tool output. Changes to code, indentation, paths, other
arguments, multiple calls, or truncated responses are not repaired. The same guard
runs after assembling streamed arguments and on non-streaming completions, without
changing tool IDs, usage, or completion status. It does not retry model requests.

On AMD devices with a GTT pool below 32 GiB, the installed 23.8 GB
`NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF` uses CPU loading. Vulkan exhausted
this machine's approximately 23 GB GTT pool even with partial offloading, ending
in `ErrorDeviceLost`. CPU loading, chat, and tool calls were verified with a
65,536-token context. Other models and hardware retain their backend selection.

### Persistent project agents

Open **Agents** from the chat header or console. Assign a name, local project folder,
goal, acceptance criteria, and configured local OpenCode model. An optional checkbox
creates a missing project directory. Assignment authorizes actual project file edits.
Qwen3 4B Instruct is initially selected when configured; model context/capability limits
still apply. This worker needs at least 16K context: the measured fixed prompt/tool
overhead was about 10K tokens, so smaller models (including the current 8K FLM
Qwen3.8 runtime) are disabled instead of starting a job that cannot fit.
A healthy FLM model that is already serving the assignment is reused. Other
selections use Hearth's normal serialized load path before execution, including
clearing a prior FLM manual-unload state.

`agent_tasks.py` supervises durable OpenCode v2 sessions through `agent_opencode.py`.
The managed `hearth-worker` agent plans, reads, edits, uses tools, verifies, and reports.
When OpenCode ends a turn without completing the target, the supervisor sends a
continuation rather than leaving the job at “I will…”. Up to 20 assignments queue;
one runs at a time to avoid local-model contention. A waiting question holds that
lane until answered or the assignment is paused. Additional instructions can be
queued while an agent runs. They are durably admitted with stable message IDs.

Assignments persist under `data/agent-tasks/`, with private filesystem permissions,
a bounded recent activity/transcript snapshot, session ID, pending input IDs, cycle
count, and verification state. Full transcripts stay in OpenCode. Closing the browser
does not cancel work. On a Hearth restart, the supervisor observes the existing
session rather than starting a duplicate, and startup preserves the agent's backend.
A process lock prevents duplicate supervisor workers. OpenCode must be installed;
Hearth starts its local service when needed. Its service credentials never reach the UI.
The setup registers the worker in the local OpenCode config, preserving existing
agents and defaults and saving a backup before the first change.

Questions render single-choice, multiple-choice, custom, and basic typed answers.
Permission requests offer **Allow once** or **Reject**. File tools run automatically;
shell commands ask unless the assignment explicitly enables automatic commands.
Commands execute as Hearth's local OS account with the project as working directory:
this is not filesystem or process sandboxing. Publishing/contacting others requires
an explicit task instruction. Agent creation and controls require an administrator
and the existing CSRF protection. All administrators can supervise assignments.

A completion claim must end with `[HEARTH_TASK_COMPLETE]` on a normally finished
assistant turn. If a verification command was assigned, the supervisor independently
runs that exact command through OpenCode's durable shell API. Only exit zero completes
the task; failures return to the agent for repair. Verification survives Hearth
restarts and can be interrupted. Without a command, completion is explicitly labelled
**agent-reported**, not independently verified. Use meaningful acceptance criteria and
a command that actually checks your target.

Pause/stop interrupt the owned session and verification command. The UI shows pending
interruption until OpenCode acknowledges it. Resume retains the goal/session. Model
management and validation runs are blocked while assignments own the execution lane;
pause agents first. The default budget is 50 continuation cycles (each OpenCode loop
can perform many tool calls); three repeated idle results or repeated errors require
review. Budget exhaustion is a blocker, never completion. Resume adds 50 cycles when
the budget was exhausted. Questions, blockers, and backend failures remain visible.

Validation:

```bash
.venv/bin/python tools/run_tests.py --browser
```

The browser test uses isolated OpenCode responses and covers real API/UI assignment,
choice answers, permissions, pause/reload/resume, completion verification and escaping.
A separate live integration check used the installed Qwen3 4B Instruct model to read
instructions, create/read a real file in a temporary project, and pass an independent
verification command.
