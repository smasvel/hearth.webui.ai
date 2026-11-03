"""Device control through the Home Assistant REST API."""
import requests


class Home:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": "Bearer " + token,
                        "Content-Type": "application/json"}

    def call(self, action: dict):
        """action = {domain, service, target, data} -> HA service call."""
        url = f"{self.url}/api/services/{action['domain']}/{action['service']}"
        payload = {"entity_id": action.get("target", [])}
        payload.update(action.get("data", {}))
        r = requests.post(url, json=payload, headers=self.headers, timeout=5)
        r.raise_for_status()

    def state(self, entity_id: str) -> dict:
        r = requests.get(f"{self.url}/api/states/{entity_id}",
                         headers=self.headers, timeout=5)
        r.raise_for_status()
        return r.json()
