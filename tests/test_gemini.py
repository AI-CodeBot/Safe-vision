import base64
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from backend.gemini import describe_live_frame, explain_incident, summarize_incident
from backend.main import emit_incidents, finish_incidents


class GeminiExplanationTests(unittest.TestCase):
    def setUp(self):
        self.event = {
            "event_type": "RESTRICTED_ZONE_ENTRY",
            "description": "P-001 entered the configured restricted zone.",
            "severity": "HIGH",
            "confidence": 0.9,
            "ai_explanation": "Deterministic explanation.",
            "recommended_action": "Review the captured frame.",
        }
        self.snapshot = tempfile.NamedTemporaryFile(delete=False)
        self.snapshot.write(b"sample image bytes")
        self.snapshot.close()
        self.addCleanup(os.unlink, self.snapshot.name)

    def test_uses_valid_gemini_explanation_and_sends_snapshot(self):
        explanation = {
            "description": "A person is visible in the marked zone.",
            "recommended_action": "Review the camera footage.",
        }
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": json.dumps(explanation)}]
                        }
                    }
                ]
            }
        ).encode()

        with (
            patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "test-model"},
            ),
            patch("backend.gemini.urllib.request.urlopen", return_value=response) as urlopen,
        ):
            result = explain_incident(self.event, self.snapshot.name)

        self.assertEqual(
            result,
            (
                "A person is visible in the marked zone.",
                "Review the camera footage.",
            ),
        )
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        image = payload["contents"][0]["parts"][1]["inline_data"]
        self.assertEqual(
            image["data"], base64.b64encode(b"sample image bytes").decode("ascii")
        )
        self.assertEqual(image["mime_type"], "image/jpeg")
        self.assertIn("/models/test-model:generateContent", request.full_url)
        self.assertNotIn("key=", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "test-key")
        self.assertIn("identify anyone", payload["contents"][0]["parts"][0]["text"])
        self.assertIn("not proof that an incident occurred", payload["contents"][0]["parts"][0]["text"])
        self.assertIn('"confidence": 0.9', payload["contents"][0]["parts"][0]["text"])

    def test_uses_deterministic_explanation_without_api_key(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("backend.gemini.urllib.request.urlopen") as urlopen,
        ):
            result = explain_incident(self.event, self.snapshot.name)

        self.assertEqual(
            result,
            ("Deterministic explanation.", "Review the captured frame."),
        )
        urlopen.assert_not_called()

    def test_uses_deterministic_explanation_when_gemini_request_fails(self):
        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch(
                "backend.gemini.urllib.request.urlopen",
                side_effect=urllib.error.URLError("offline"),
            ),
        ):
            result = explain_incident(self.event, self.snapshot.name)

        self.assertEqual(
            result,
            ("Deterministic explanation.", "Review the captured frame."),
        )

    def test_summary_uses_gemini_and_requires_exact_source_times(self):
        event = {
            **self.event,
            "started_at_seconds": 1.25,
            "ended_at_seconds": 3.5,
            "duration_seconds": 2.25,
        }
        summary = "A person entered the zone. Started at 1.25s, ended at 3.5s, duration 2.25s."
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": json.dumps({"summary": summary})}]
                        }
                    }
                ]
            }
        ).encode()

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch("backend.gemini.urllib.request.urlopen", return_value=response) as urlopen,
        ):
            result = summarize_incident(event, self.snapshot.name)

        self.assertEqual(result, summary)
        prompt = json.loads(urlopen.call_args.args[0].data)["contents"][0]["parts"][0]["text"]
        self.assertIn("original source-video timestamps", prompt)
        self.assertIn('"ended_at_seconds": "3.5s"', prompt)

    def test_summary_falls_back_when_gemini_omits_a_source_time(self):
        event = {
            **self.event,
            "started_at_seconds": 1.25,
            "ended_at_seconds": 3.5,
            "duration_seconds": 2.25,
        }
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": json.dumps({"summary": "A person entered the zone."})}]
                        }
                    }
                ]
            }
        ).encode()

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch("backend.gemini.urllib.request.urlopen", return_value=response),
        ):
            result = summarize_incident(event, self.snapshot.name)

        self.assertIn("Started at 1.25s, ended at 3.5s, and lasted 2.25s", result)

    def test_describes_live_frame_without_incident_data(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "candidates": [
                    {"content": {"parts": [{"text": "A chair is visible near a table."}]}}
                ]
            }
        ).encode()

        with (
            patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "test-model"},
            ),
            patch("backend.gemini.urllib.request.urlopen", return_value=response) as urlopen,
        ):
            answer = describe_live_frame(b"camera frame", "What is visible?")

        self.assertEqual(answer, "A chair is visible near a table.")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        image = payload["contents"][0]["parts"][1]["inline_data"]
        self.assertEqual(
            image["data"], base64.b64encode(b"camera frame").decode("ascii")
        )
        self.assertEqual(image["mime_type"], "image/jpeg")
        self.assertIn("What is visible?", payload["contents"][0]["parts"][0]["text"])
        self.assertIn("Do not identify people", payload["contents"][0]["parts"][0]["text"])
        self.assertIn("/models/test-model:generateContent", request.full_url)

    def test_live_frame_reports_missing_gemini_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                describe_live_frame(b"camera frame", "What is visible?")

    def test_active_incident_does_not_schedule_summary(self):
        persisted_incident = {
            "id": "incident-1",
            "snapshot_path": self.snapshot.name,
            "event_type": self.event["event_type"],
            "created_now": True,
        }
        frame = MagicMock()

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch("backend.main.upsert_active_incident", return_value=persisted_incident),
            patch("backend.main.gemini_executor.submit") as submit,
        ):
            result = emit_incidents(
                [self.event],
                frame,
                source_type="camera",
                source_id="session-1",
                session_id="session-1",
            )

        submit.assert_not_called()
        frame.copy.assert_called_once()
        self.assertEqual(result[0]["id"], "incident-1")
        self.assertTrue(result[0]["is_new"])

    def test_finished_incident_claims_and_schedules_one_summary(self):
        incident = {
            "id": "incident-1",
            "snapshot_path": self.snapshot.name,
            "timestamp_seconds": 1.25,
            "ended_at_seconds": 3.5,
            "duration_seconds": 2.25,
        }
        ended_event = {
            **self.event,
            "started_at_seconds": 1.25,
            "ended_at_seconds": 3.5,
            "duration_seconds": 2.25,
        }
        with (
            patch("backend.main.finish_active_incident", return_value=incident),
            patch("backend.main.claim_incident_summary", return_value=True) as claim,
            patch("backend.main.gemini_executor.submit") as submit,
        ):
            finish_incidents([ended_event], source_id="session-1")

        claim.assert_called_once_with("incident-1")
        submit.assert_called_once_with(
            unittest.mock.ANY,
            "incident-1",
            ended_event,
            self.snapshot.name,
        )


if __name__ == "__main__":
    unittest.main()
