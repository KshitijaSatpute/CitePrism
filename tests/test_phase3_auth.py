import unittest

from src.webapi.auth import AuthenticationError, authenticate

TENANT = "00000000-0000-4000-8000-000000000001"


class AuthenticationTests(unittest.TestCase):
    def test_local_mode_has_explicit_development_identity(self):
        value = authenticate(
            mode="local",
            tenant=None,
            actor=None,
            roles=None,
            supplied_proxy_secret=None,
            configured_tenant=TENANT,
            configured_proxy_secret=None,
        )
        self.assertEqual(value.actor_id, "local-reviewer")
        self.assertTrue(value.can("reviewer"))

    def test_proxy_mode_fails_closed_and_uses_verified_claims(self):
        with self.assertRaises(AuthenticationError):
            authenticate(
                mode="trusted_proxy",
                tenant=TENANT,
                actor="person",
                roles="reviewer",
                supplied_proxy_secret="wrong",
                configured_tenant=TENANT,
                configured_proxy_secret="correct",
            )
        value = authenticate(
            mode="trusted_proxy",
            tenant=TENANT,
            actor="person",
            roles="reviewer",
            supplied_proxy_secret="correct",
            configured_tenant=TENANT,
            configured_proxy_secret="correct",
        )
        self.assertEqual(value.actor_id, "person")
        self.assertFalse(value.can("operator"))


if __name__ == "__main__":
    unittest.main()
