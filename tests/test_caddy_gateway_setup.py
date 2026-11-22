import unittest

from tools import caddy_gateway_setup as gateway


class CaddyGatewaySetupTests(unittest.TestCase):
    def test_tokens_are_64_hex_and_existing_ones_survive(self):
        tokens = gateway.generate_tokens()
        self.assertEqual(set(tokens), set(gateway.TOKEN_LABELS))
        for token in tokens.values():
            self.assertRegex(token, r"^[0-9a-f]{64}$")
        kept = gateway.generate_tokens({"lan": "a" * 64})
        self.assertEqual(kept["lan"], "a" * 64)
        self.assertNotEqual(kept["opencode"], tokens["opencode"])

    def test_render_has_every_backend_address_and_token(self):
        tokens = gateway.generate_tokens()
        text = gateway.render_caddyfile(["192.0.2.10", "localhost", "127.0.0.1"], tokens)
        self.assertIn('@opencode header Authorization "Bearer ' + tokens["opencode"] + '"', text)
        self.assertIn('@vscode header Authorization "Bearer ' + tokens["vscode"] + '"', text)
        self.assertIn('@lan header Authorization "Bearer ' + tokens["lan"] + '"', text)
        self.assertIn("192.0.2.10:9443, localhost:9443, 127.0.0.1:9443", text)
        for _name, port, upstream in gateway.BACKENDS:
            self.assertIn(f":{port}", text)
            self.assertIn(f"reverse_proxy @lan localhost:{upstream}", text)
        self.assertEqual(text.count("tls internal"), len(gateway.BACKENDS))

    def test_parse_tokens_round_trips_and_ignores_other_headers(self):
        tokens = gateway.generate_tokens()
        text = gateway.render_caddyfile(["localhost"], tokens)
        parsed = gateway.parse_tokens(text.replace(tokens["lan"], "b" * 64))
        self.assertEqual(parsed["lan"], "b" * 64)
        self.assertEqual(set(parsed), set(gateway.TOKEN_LABELS))

    def test_install_section_replaces_managed_block_and_keeps_other_sites(self):
        other = "example.test {\n\trespond \"ok\"\n}\n"
        first = gateway.install_section(other, "first")
        self.assertIn("first", first)
        self.assertIn(other, first)
        second = gateway.install_section(first, "second")
        self.assertIn("second", second)
        self.assertNotIn("first", second)
        self.assertIn(other, second)
        self.assertEqual(second.count(gateway.BEGIN_MARKER), 1)

    def test_legacy_marked_file_is_left_untouched(self):
        legacy = "example.test {\n}\n\n" + gateway.LEGACY_MARKER + "\nold snippet\n"
        self.assertEqual(gateway.install_section(legacy, "new"), legacy)

    def test_install_section_is_idempotent(self):
        text = gateway.render_caddyfile(["localhost"], gateway.generate_tokens())
        once = gateway.install_section("", text)
        self.assertEqual(gateway.install_section(once, text), once)


if __name__ == "__main__":
    unittest.main()
