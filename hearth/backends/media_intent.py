"""Small intent detectors for chat's image and video request handling."""

from __future__ import annotations

import re

_IMAGE_NOUNS = (
    "image", "images", "picture", "pictures", "photo", "photos", "photograph", "photographs",
    "drawing", "drawings", "painting", "paintings", "illustration", "illustrations",
    "artwork", "art", "wallpaper", "wallpapers", "logo", "logos", "icon", "icons",
    "avatar", "avatars", "portrait", "portraits", "poster", "posters", "meme", "memes",
    "sticker", "stickers", "graphic", "graphics", "thumbnail", "thumbnails", "sketch", "sketches",
)
_IMAGE_IDIOM_EXCLUDE_RE = re.compile(
    r"\bdraw\s+(?:(?:a|an|my|your|the)\s+)?(?:\w+\s+){0,2}"
    r"(?:conclusion|comparison|attention|blank|line|parallel|distinction|analogy|inference)s?\b"
    r"|\bsketch\s+(?:out\s+)?(?:(?:a|an|my|your|the|some)\s+)?(?:\w+\s+){0,2}"
    r"(?:plan|outline|idea|strategy|approach|solution|roadmap|framework|budget|script|"
    r"letter|email|response|message|list|agenda)s?\b", re.IGNORECASE,
)
_IMAGE_REQUEST_RE = re.compile(
    r"\b(?:draw|paint|doodle|sketch)\s+(?!a\s+(?:conclusion|comparison|blank|line|parallel|distinction|analogy|inference))"
    r"(?:(?:me|us)\s+)?"
    r"|\b(?:draw|paint|doodle|sketch)\b"
    r"|\b(?:generate|create|make|design|render|produce|illustrate|show me|give me|draw me|paint me)\b"
    r"[^.!?\n]{0,40}?\b(?:" + "|".join(_IMAGE_NOUNS) + r")s?\b"
    r"|\b(?:" + "|".join(_IMAGE_NOUNS) + r")\s+(?:of|showing|depicting|featuring)\b",
    re.IGNORECASE,
)
_IMAGE_FOLLOWUP_RE = re.compile(
    r"\bnot\s+(?:clear|sharp|good|right|accurate)\b|\bisn'?t\s+(?:clear|sharp|right|working)\b"
    r"|\b(?:blurry|blurred|unclear|pixelated|distorted|deformed)\b|\btoo\s+(?:blurry|dark|small|low\s+res(?:olution)?)\b"
    r"|\b(?:that'?s|this\s+is|it'?s|\w+\s+(?:is|looks?))\s+wrong\b|\bwrong\s+(?:face|hand|hands|pose|proportions?|colou?rs?)\b"
    r"|\b(?:redo|regenerate|re-?generate)\s*(?:it|this)?\b|\btry\s+again\b"
    r"|\bone\s+more\s+(?:try|attempt|go)\b|\bfix\s+(?:it|this|the)\b"
    r"|\b(?:make|render)\s+it\s+(?:sharper|clearer|better|more\s+\w+)\b"
    r"|\bmore\s+(?:detailed|accurate|realistic)\b|\bhigher\s+(?:quality|resolution)\b"
    r"|\bdifferent\s+(?:pose|style|angle|background)\b", re.IGNORECASE,
)
_VIDEO_REQUEST_RE = re.compile(
    r"\b(?:generate|create|make|render|produce)\b[^.!?\n]{0,50}\b(?:video|movie|clip|animation)\b"
    r"|\b(?:video|movie|clip|animation)\s+(?:of|showing|depicting|featuring)\b", re.IGNORECASE,
)


def looks_like_image_request(text: str | None) -> bool:
    return bool(text and not _IMAGE_IDIOM_EXCLUDE_RE.search(text) and _IMAGE_REQUEST_RE.search(text))


def looks_like_image_followup(text: str | None) -> bool:
    return bool(text and _IMAGE_FOLLOWUP_RE.search(text))


def looks_like_video_request(text: str | None) -> bool:
    return bool(text and _VIDEO_REQUEST_RE.search(text))
