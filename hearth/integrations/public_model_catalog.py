"""Search the public Lemonade and LM Studio catalogs for downloadable GGUF models."""

from functools import lru_cache
from html.parser import HTMLParser
import re
import time
import requests


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.href = None
        self.label = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = []

    def handle_data(self, data):
        if self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.links.append((" ".join(" ".join(self.label).split()), self.href))
            self.href = None


def _links(url):
    response = requests.get(url, timeout=8)
    response.raise_for_status()
    parser = _Links()
    parser.feed(response.text)
    return parser.links


@lru_cache(maxsize=1)
def _lemonade_catalog_bucket(hour):
    release = requests.get("https://api.github.com/repos/lemonade-sdk/lemonade/releases/latest", timeout=8)
    release.raise_for_status()
    tag = release.json()["tag_name"]
    if not re.fullmatch(r"v[0-9][A-Za-z0-9._-]*", tag):
        raise ValueError("Invalid Lemonade release tag")
    url = f"https://raw.githubusercontent.com/lemonade-sdk/lemonade/{tag}/src/cpp/resources/server_models.json"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


def lemonade_gguf(query, known_ids, limit=80):
    """Supplement a server's older catalog with current public GGUF entries."""
    catalog = _lemonade_catalog_bucket(int(time.time() // 3600))
    words = query.casefold().split()
    known = {value.casefold() for value in known_ids}
    rows = []
    for model_id, item in catalog.items():
        checkpoint = (item.get("checkpoint") or (item.get("checkpoints") or {}).get("main") or "").split(":", 1)[0]
        if (model_id.casefold() in known or checkpoint.casefold() in known
                or item.get("recipe") not in ("llamacpp", "llamacpp-rocm")
                or not re.fullmatch(r"[\w.-]+/[\w.-]+", checkpoint)):
            continue
        haystack = " ".join((model_id, checkpoint, " ".join(item.get("labels") or []))).casefold()
        if all(word in haystack for word in words):
            rows.append({"id": checkpoint, "checkpoint": checkpoint, "source": "huggingface",
                         "catalog_name": model_id, "recipe": "llamacpp", "labels": item.get("labels") or [],
                         "size_gb": item.get("size"), "suggested": bool(item.get("suggested"))})
    rows.sort(key=lambda row: (not row["suggested"], row["catalog_name"]))
    return rows[:limit]


@lru_cache(maxsize=2)
def _lm_families_bucket(hour):
    links = _links("https://lmstudio.ai/models?filter=tools")
    return [(label, href) for label, href in links
            if re.fullmatch(r"/models/[\w.-]+", href)]


@lru_cache(maxsize=256)
def _lm_family_repos(href, hour):
    repos = []
    for _, artifact in _links("https://lmstudio.ai" + href):
        if not re.fullmatch(r"/models/[\w.-]+/[\w.-]+", artifact):
            continue
        for _, source in _links("https://lmstudio.ai" + artifact):
            if re.fullmatch(r"https://huggingface.co/[\w.-]+/[\w.-]+", source):
                repos.append(source)
    return list(dict.fromkeys(repos))


def lmstudio_gguf(query, limit=40):
    """Find downloadable local artifacts through LM Studio's published model families."""
    words = query.casefold().split()
    hour = int(time.time() // 3600)
    rows = []
    matched_families = 0
    for label, href in _lm_families_bucket(hour):
        if not all(word in (label + " " + href).casefold() for word in words):
            continue
        matched_families += 1
        if not words and matched_families > 12:
            break
        try:
            sources = _lm_family_repos(href, hour)
        except (requests.RequestException, ValueError):
            continue
        for source in sources:
            repo = source.removeprefix("https://huggingface.co/")
            if any(row["id"].casefold() == repo.casefold() for row in rows):
                continue
            rows.append({"id": repo, "checkpoint": repo, "source": "huggingface",
                         "source_url": source, "labels": ["tool-calling"], "description": label})
            if len(rows) >= limit:
                return rows
    return rows
