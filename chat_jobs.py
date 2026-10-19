"""Browser-independent chat jobs and per-account progress snapshots."""
from __future__ import annotations

import json
import threading
import time
import uuid


def cancelable_lines(lines, cancel):
    try:
        for line in lines:
            if cancel.is_set():
                break
            yield line
    finally:
        if hasattr(lines, "close"):
            lines.close()


class ChatJob:
    def __init__(self, user_id, conversation_id, model, user_text, base_message_count):
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.id = uuid.uuid4().hex
        self.model = model
        self.user_text = user_text
        self.base_message_count = base_message_count
        self.user_saved = False
        self.started_at = time.time()
        self.finished_at = None
        self.status = "running"
        self.content = ""
        self.reasoning = ""
        self.error = None
        self.cancel = threading.Event()
        self.lock = threading.RLock()

    def snapshot(self):
        with self.lock:
            return {
                "id": self.id, "status": self.status, "model": self.model,
                "content": self.content, "reasoning": self.reasoning,
                "error": self.error, "started_at": self.started_at,
                "user_text": self.user_text, "base_message_count": self.base_message_count,
                "stop_requested": self.cancel.is_set(),
            }

    def consume(self, response):
        """Drain SSE even when every browser has left; saving happens in the producer."""
        buffer = ""
        for chunk in response.response:
            buffer += chunk.decode() if isinstance(chunk, bytes) else chunk
            events = buffer.split("\n\n")
            buffer = events.pop()
            for event in events:
                for line in event.splitlines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        payload = json.loads(line[5:].strip())
                    except (ValueError, TypeError):
                        continue
                    with self.lock:
                        choices = payload.get("choices") or []
                        delta = (choices[0].get("delta") or {}) if choices else {}
                        self.content += delta.get("content") or ""
                        self.reasoning += delta.get("reasoning_content") or ""
                        self.content += payload.get("screenshots_markdown") or ""
                        if payload.get("error"):
                            self.error = str(payload["error"])


class JobRegistry:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}

    def get(self, user_id, conversation_id):
        with self.lock:
            job = self.jobs.get(conversation_id)
            return job if job and job.user_id == user_id else None

    def start(self, user_id, conversation_id, model, user_text, base_message_count, run):
        with self.lock:
            now = time.time()
            for key, old in list(self.jobs.items()):
                if old.finished_at and now - old.finished_at > 3600:
                    del self.jobs[key]
            old = self.jobs.get(conversation_id)
            if old and (old.user_id != user_id or old.status == "running"):
                raise ValueError("This conversation already has a response in progress.")
            job = ChatJob(user_id, conversation_id, model, user_text, base_message_count)
            self.jobs[conversation_id] = job

        def worker():
            try:
                if not job.cancel.is_set():
                    run(job)
            except Exception as exc:
                with job.lock:
                    job.error = str(exc)
            finally:
                with job.lock:
                    job.status = "stopped" if job.cancel.is_set() else ("error" if job.error else "completed")
                    job.finished_at = time.time()

        threading.Thread(target=worker, daemon=True).start()
        return job


registry = JobRegistry()
