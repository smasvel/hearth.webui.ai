"""Track a single proxied generation so a local interrupt can close its socket."""
import socket
import threading
import time


class Registry:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = {}
        self.cancelled = {}

    def begin(self, key):
        if not key:
            return
        with self.lock:
            if key not in self.cancelled:
                self.active[key] = None

    def attach(self, key, response):
        if not key:
            return
        with self.lock:
            if key not in self.active:
                cancelled = True
            else:
                self.active[key] = response
                cancelled = False
        if cancelled:
            self._close(response)

    def cancel(self, key):
        with self.lock:
            response = self.active.pop(key, None)
            self.cancelled[key] = time.monotonic()
            self.cancelled = {item: stamp for item, stamp in self.cancelled.items()
                              if time.monotonic() - stamp < 120}
        if response is not None:
            self._close(response)

    def finish(self, key):
        with self.lock:
            self.active.pop(key, None)

    @staticmethod
    def _close(response):
        # response.close() alone need not wake a thread blocked inside iter_lines().
        raw = getattr(response, 'raw', None)
        try:
            sock = raw._fp.fp.raw._sock
            sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError, ValueError):
            pass
        response.close()


streams = Registry()
