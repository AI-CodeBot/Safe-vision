import unittest
from unittest.mock import patch

from backend.auth import (
    SESSION_MAX_AGE,
    create_session_token,
    is_valid_session_token,
    validate_auth_configuration,
)


class SessionTokenTests(unittest.TestCase):
    def setUp(self):
        self.password = "test-only-access-password"

    def test_session_token_is_valid_until_expiration(self):
        now = 1_800_000_000
        token = create_session_token(self.password, now=now)

        self.assertTrue(is_valid_session_token(token, self.password, now=now))
        self.assertTrue(
            is_valid_session_token(
                token, self.password, now=now + SESSION_MAX_AGE
            )
        )
        self.assertFalse(
            is_valid_session_token(
                token, self.password, now=now + SESSION_MAX_AGE + 1
            )
        )

    def test_session_token_rejects_tampering_wrong_password_and_future_issue(self):
        now = 1_800_000_000
        token = create_session_token(self.password, now=now)
        payload, signature = token.rsplit(".", 1)
        changed_signature = ("0" if signature[0] != "0" else "1") + signature[1:]

        self.assertFalse(
            is_valid_session_token(
                f"{payload}.{changed_signature}", self.password, now=now
            )
        )
        self.assertFalse(
            is_valid_session_token(token, "different-password", now=now)
        )
        self.assertFalse(is_valid_session_token(token, self.password, now=now - 1))
        self.assertFalse(
            is_valid_session_token("not-a-session-token", self.password, now=now)
        )

    def test_production_requires_a_configured_password(self):
        with patch.dict(
            "os.environ",
            {"SAFEVISION_ENV": "production"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "is required"):
                validate_auth_configuration()

    def test_development_can_run_without_auth_configuration(self):
        with patch.dict("os.environ", {"SAFEVISION_ENV": "development"}, clear=True):
            validate_auth_configuration()


if __name__ == "__main__":
    unittest.main()
