import re
import unittest

from session_cookie import session_cookie_name


class SessionCookieIsolationTests(unittest.TestCase):
    def test_each_port_gets_its_own_cookie_name(self):
        """Browsers ignore ports when scoping cookies, so the name must carry the isolation."""
        self.assertEqual(session_cookie_name(5443), "hearth_session_5443")
        self.assertEqual(session_cookie_name(5444), "hearth_session_5444")
        self.assertNotEqual(session_cookie_name(5443), session_cookie_name(5444))

    def test_name_is_a_valid_stable_cookie_token(self):
        for port in (5443, 5444, 10443):
            name = session_cookie_name(port)
            self.assertRegex(name, r"^[A-Za-z0-9_]+$")
            self.assertEqual(name, session_cookie_name(port))

    def test_non_numeric_input_cannot_produce_an_empty_or_unsafe_name(self):
        self.assertEqual(session_cookie_name("5443"), "hearth_session_5443")
        self.assertRegex(session_cookie_name(""), r"^hearth_session_\d+$")
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9_]+", session_cookie_name("54;43")))

    def test_two_flask_apps_with_different_ports_do_not_share_a_cookie(self):
        from flask import Flask

        def client_for(port):
            application = Flask(f"hearth-{port}")
            application.secret_key = "test-key"
            application.config["SESSION_COOKIE_NAME"] = session_cookie_name(port)
            client = application.test_client()
            with client.session_transaction() as session:
                session["logged_in"] = True
            return client

        first = client_for(5443)
        second = client_for(5444)
        self.assertIsNotNone(first.get_cookie("hearth_session_5443"))
        self.assertIsNone(first.get_cookie("hearth_session_5444"))
        self.assertIsNotNone(second.get_cookie("hearth_session_5444"))
        self.assertIsNone(second.get_cookie("hearth_session_5443"))


if __name__ == "__main__":
    unittest.main()
