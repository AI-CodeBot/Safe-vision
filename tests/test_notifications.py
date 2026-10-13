import json
import os
import unittest
from unittest.mock import MagicMock, patch

from backend.notifications import send_security_alert


class SecurityAlertTests(unittest.TestCase):
    def setUp(self):
        self.incident = {
            "id": "incident-123",
            "event_type": "RESTRICTED_ZONE_ENTRY",
            "severity": "HIGH",
            "description": "A person entered the restricted area.",
            "recommended_action": "Verify the area.",
        }
        self.settings = {
            "RESEND_API_KEY": "test-key",
            "RESEND_FROM_EMAIL": "alerts@example.com",
            "SECURITY_ALERT_EMAILS": "guard1@example.com, guard2@example.com",
        }

    def test_sends_incident_details_to_configured_guard_recipients(self):
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        with (
            patch.dict(os.environ, self.settings, clear=True),
            patch("backend.notifications.urlopen", return_value=response) as urlopen,
        ):
            send_security_alert(self.incident)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.resend.com/emails")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        payload = json.loads(request.data)
        self.assertEqual(payload["to"], ["guard1@example.com", "guard2@example.com"])
        self.assertEqual(payload["from"], "alerts@example.com")
        self.assertIn("RESTRICTED ZONE ENTRY", payload["subject"].upper())
        self.assertIn(self.incident["recommended_action"], payload["text"])

    def test_requires_provider_and_recipient_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "Security email is not configured"):
                send_security_alert(self.incident)


if __name__ == "__main__":
    unittest.main()