import unittest

from fastapi import HTTPException

from backend.events import IncidentEngine
from backend.main import validate_settings


class DetectionSettingsTests(unittest.TestCase):
    def test_defaults_keep_rules_enabled_with_strict_confirmation(self):
        settings = validate_settings({})

        self.assertEqual(settings["incident_confidence"], 0.9)
        self.assertEqual(settings["confirmation_samples"], 3)
        self.assertEqual(
            settings["incident_rules"],
            {
                "restricted_zone_entry": True,
                "prolonged_presence": True,
                "crowd_threshold_exceeded": True,
            },
        )
        self.assertIsNone(settings["restricted_zone"])

    def test_validation_allows_stricter_confidence_and_more_samples(self):
        settings = validate_settings(
            {"incident_confidence": 0.97, "confirmation_samples": 8}
        )

        self.assertEqual(settings["incident_confidence"], 0.97)
        self.assertEqual(settings["confirmation_samples"], 8)

    def test_validation_rejects_weaker_confidence_and_invalid_confirmation(self):
        for settings in (
            {"incident_confidence": 0.89},
            {"incident_confidence": True},
            {"confirmation_samples": 2},
            {"confirmation_samples": 11},
            {"confirmation_samples": True},
            {"incident_rules": {"crowd_threshold_exceeded": "yes"}},
        ):
            with self.subTest(settings=settings):
                with self.assertRaises(HTTPException):
                    validate_settings(settings)

    def test_rule_switch_prevents_disabled_rule_from_emitting(self):
        engine = IncidentEngine()
        crowd = [
            {
                "label": "person",
                "confidence": 0.99,
                "person_id": f"P-{index}",
                "bbox": [index * 10, 20, index * 10 + 8, 90],
                "track": {"duration_seconds": 0},
            }
            for index in range(3)
        ]
        rules = {
            "restricted_zone_entry": False,
            "prolonged_presence": False,
            "crowd_threshold_exceeded": False,
        }

        for timestamp in range(5):
            events = engine.update(
                crowd,
                timestamp=timestamp,
                width=100,
                height=100,
                crowd_threshold=3,
                confirmation_samples=3,
                incident_rules=rules,
            )
            self.assertEqual(events, [])

    def test_runtime_threshold_changes_reset_pending_confirmation(self):
        engine = IncidentEngine()
        crowd = [
            {
                "label": "person",
                "confidence": 0.92,
                "person_id": f"P-{index}",
                "bbox": [index * 10, 20, index * 10 + 8, 90],
                "track": {"duration_seconds": 0},
            }
            for index in range(3)
        ]
        options = {
            "timestamp": 0,
            "width": 100,
            "height": 100,
            "crowd_threshold": 3,
            "confirmation_samples": 3,
        }

        for timestamp in range(2):
            self.assertEqual(
                engine.update(crowd, **{**options, "timestamp": timestamp}),
                [],
            )
        self.assertEqual(
            engine.update(
                crowd,
                **{
                    **options,
                    "timestamp": 2,
                    "confidence_threshold": 0.95,
                },
            ),
            [],
        )
        crowd = [{**item, "confidence": 0.96} for item in crowd]
        for timestamp in (3, 4):
            self.assertEqual(
                engine.update(crowd, **{**options, "timestamp": timestamp, "confidence_threshold": 0.95}),
                [],
            )
        result = engine.update(
            crowd,
            **{**options, "timestamp": 5, "confidence_threshold": 0.95},
        )

        self.assertEqual(
            [event["event_type"] for event in result],
            ["CROWD_THRESHOLD_EXCEEDED"],
        )


if __name__ == "__main__":
    unittest.main()
