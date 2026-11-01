"""Common test helper to exercise Hearth's backend paths on either side."""
from __future__ import annotations

import time
from unittest.mock import patch

from email.utils import formatdate


def patch_admin():
    import app
    return patch.object(app.users_store, "get_user_by_id", side_effect=lambda uid: {"id": uid, "username": uid, "role": "admin"})


def as_admin():
    """Return a Flask test client authenticated as a probe admin."""
    import app
    client = app.app.test_client()
    with client.session_transaction() as s:
        s.update(
            logged_in=True, user_id="probe", username="probe", role="admin",
            last_activity=time.time(), login_csrf_token="csrf-probe",
        )
    return client


def csrf():
    return {"X-CSRF-Token": "csrf-probe"}


def report(label, value=None):
    print(f"    {label}{'  ->  ' + str(value) if value is not None else ''}")
