import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from backend import database as incident_database
from backend.events import DEFAULT_CONFIRMATION_SAMPLES, IncidentEngine
from backend.tracker import PersonTracker


def person(bbox=(20, 20, 60, 90), confidence=0.9):
    return {
        "label": "person",
        "bbox": list(bbox),
        "confidence": confidence,
        "person_id": "P-001",
        "track": {"duration_seconds": 0},
    }


class PersonTrackerTests(unittest.TestCase):
    def test_overlapping_person_keeps_stable_id_and_duration(self):
        tracker = PersonTracker()
        first = tracker.update([person()], 0.0)[0]
        second = tracker.update([person((22, 20, 62, 90))], 2.0)[0]

        self.assertEqual(first["person_id"], second["person_id"])
        self.assertEqual(second["track"]["duration_seconds"], 2.0)

    def test_track_is_replaced_after_person_has_left(self):
        tracker = PersonTracker(max_gap_seconds=1)
        first = tracker.update([person()], 0.0)[0]["person_id"]
        next_person = tracker.update([person((20, 20, 60, 90))], 2.0)[0]["person_id"]

        self.assertNotEqual(first, next_person)


class IncidentPersistenceTests(unittest.TestCase):
    def test_active_incident_updates_once_and_summary_can_only_be_claimed_once(self):
        event = {
            "event_type": "PROLONGED_PRESENCE",
            "started_at_seconds": 2.0,
            "last_seen_timestamp_seconds": 4.0,
            "duration_seconds": 2.0,
            "severity": "MEDIUM",
            "confidence": 0.95,
            "person_id": "P-001",
            "description": "P-001 remained visible.",
            "ai_explanation": "Pending completion.",
            "recommended_action": "Review the footage.",
        }
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "incidents.sqlite3"
            with patch.object(incident_database, "DATABASE_PATH", database_path):
                incident_database.initialize_database()
                session_id = incident_database.create_session("camera", "test")
                first = incident_database.upsert_active_incident(
                    event=event,
                    source_type="camera",
                    source_id="source-1",
                    session_id=session_id,
                    snapshot=None,
                )
                updated = incident_database.upsert_active_incident(
                    event={
                        **event,
                        "last_seen_timestamp_seconds": 6.0,
                        "duration_seconds": 4.0,
                    },
                    source_type="camera",
                    source_id="source-1",
                    session_id=session_id,
                    snapshot=None,
                )

                self.assertEqual(first["id"], updated["id"])
                self.assertEqual(updated["timestamp_seconds"], 2.0)
                self.assertEqual(updated["duration_seconds"], 4.0)

                finished = incident_database.finish_active_incident(
                    event={
                        **event,
                        "ended_at_seconds": 7.0,
                        "duration_seconds": 5.0,
                    },
                    source_id="source-1",
                )
                self.assertEqual(finished["ended_at_seconds"], 7.0)
                self.assertEqual(finished["duration_seconds"], 5.0)
                self.assertEqual(finished["is_active"], 0)
                self.assertTrue(incident_database.claim_incident_summary(first["id"]))
                self.assertFalse(incident_database.claim_incident_summary(first["id"]))
                self.assertTrue(
                    incident_database.mark_incident_security_alerted(first["id"])
                )
                self.assertFalse(
                    incident_database.mark_incident_security_alerted(first["id"])
                )
                self.assertIsNotNone(
                    incident_database.get_incident(first["id"])["security_alerted_at"]
                )

                next_incident = incident_database.upsert_active_incident(
                    event=event,
                    source_type="camera",
                    source_id="source-1",
                    session_id=session_id,
                    snapshot=None,
                )
                self.assertNotEqual(first["id"], next_incident["id"])


class IncidentEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = IncidentEngine()
        self.zone = {"x": 0.25, "y": 0.1, "width": 0.5, "height": 0.8}

    def process(self, detections, timestamp, **settings):
        zone = settings.pop("zone", self.zone)
        options = {
            "prolonged_seconds": 5,
            "crowd_threshold": 3,
        }
        options.update(settings)
        return self.engine.update(
            detections,
            timestamp=timestamp,
            width=100,
            height=100,
            zone=zone,
            **options,
        )

    def test_zone_entry_is_emitted_once_until_exit_and_reentry(self):
        inside = person((40, 20, 60, 90))
        outside = person((0, 20, 15, 90))

        initial_inside = self.process([inside], 0)
        self.process([outside], 1)
        self.process([outside], 2)
        self.process([outside], 2.5)
        first_inside = self.process([inside], 3)
        self.assertEqual(first_inside, [])
        self.assertEqual(self.process([person((42, 20, 62, 90))], 4), [])
        reentry = self.process([person((43, 20, 63, 90))], 5)

        self.assertEqual(initial_inside, [])
        self.assertEqual(
            [event["event_type"] for event in reentry],
            ["RESTRICTED_ZONE_ENTRY"],
        )
        self.assertIn(
            f"{DEFAULT_CONFIRMATION_SAMPLES} sampled frames",
            reentry[0]["description"],
        )

    def test_zone_entry_requires_minimum_detection_confidence(self):
        low_confidence = person((40, 20, 60, 90), confidence=0.89)
        qualified = person((40, 20, 60, 90), confidence=0.9)

        self.assertEqual(self.process([low_confidence], 0), [])
        self.assertEqual(self.process([person((0, 20, 15, 90))], 1), [])
        self.assertEqual(self.process([person((0, 20, 15, 90))], 2), [])
        self.assertEqual(self.process([person((0, 20, 15, 90))], 2.5), [])
        self.assertEqual(self.process([qualified], 3), [])
        self.assertEqual(self.process([qualified], 4), [])
        result = self.process([qualified], 5)

        self.assertEqual(
            [event["event_type"] for event in result],
            ["RESTRICTED_ZONE_ENTRY"],
        )

    def test_no_restricted_zone_incident_is_emitted_without_configured_zone(self):
        inside_frame = person((40, 20, 60, 90))

        for timestamp in range(DEFAULT_CONFIRMATION_SAMPLES):
            self.assertEqual(
                self.process([inside_frame], timestamp, zone=None),
                [],
            )

    def test_changing_zone_does_not_report_people_already_inside_as_entries(self):
        outside = person((0, 20, 15, 90))
        inside_new_zone = person((40, 20, 60, 90))
        changed_zone = {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}

        self.process([outside], 0)
        self.process([outside], 1)
        self.assertEqual(
            self.process([inside_new_zone], 2, zone=changed_zone),
            [],
        )
        self.assertEqual(
            self.process([inside_new_zone], 3, zone=changed_zone),
            [],
        )
        self.assertEqual(
            self.process([inside_new_zone], 4, zone=changed_zone),
            [],
        )

    def test_prolonged_presence_triggers_once_at_threshold(self):
        detections = [
            {**person(), "person_id": "P-001", "track": {"duration_seconds": 5}}
        ]

        first = self.process(detections, 5)
        second = self.process(
            [{**person(), "person_id": "P-001", "track": {"duration_seconds": 6}}],
            6,
        )
        third = self.process(
            [{**person(), "person_id": "P-001", "track": {"duration_seconds": 7}}],
            7,
        )
        repeated = self.process(
            [{**person(), "person_id": "P-001", "track": {"duration_seconds": 8}}],
            8,
        )

        self.assertNotIn("PROLONGED_PRESENCE", [event["event_type"] for event in first])
        self.assertNotIn("PROLONGED_PRESENCE", [event["event_type"] for event in second])
        self.assertIn("PROLONGED_PRESENCE", [event["event_type"] for event in third])
        self.assertNotIn("PROLONGED_PRESENCE", [event["event_type"] for event in repeated])
        active = self.engine.active_events()[0]
        self.assertEqual(active["last_seen_timestamp_seconds"], 8)
        self.assertEqual(active["duration_seconds"], 3)
        ended = self.engine.finalize()[0]
        self.assertEqual(ended["ended_at_seconds"], 8)
        self.assertEqual(ended["duration_seconds"], 3)

    def test_zone_incident_keeps_source_times_through_tracking_grace(self):
        outside = person((0, 20, 15, 90))
        inside = person((40, 20, 60, 90))

        for timestamp in (0, 1, 2):
            self.process([outside], timestamp)
        self.process([inside], 3)
        self.process([inside], 4)
        started = self.process([inside], 5)

        self.assertEqual(
            [event["event_type"] for event in started],
            ["RESTRICTED_ZONE_ENTRY"],
        )
        active = self.engine.active_events()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["started_at_seconds"], 3)
        self.assertEqual(active[0]["duration_seconds"], 2)

        self.process([], 8)
        self.assertEqual(len(self.engine.active_events()), 1)
        self.process([], 11)
        self.assertEqual(self.engine.active_events(), [])
        ended = self.engine.drain_ended_events()
        self.assertEqual(ended[0]["ended_at_seconds"], 5)
        self.assertEqual(ended[0]["duration_seconds"], 2)

    def test_crowd_incident_is_rearmed_only_after_count_falls(self):
        crowd = [
            {**person((index * 10, 20, index * 10 + 8, 90)), "person_id": f"P-{index}"}
            for index in range(3)
        ]

        first = self.process(crowd, 1)
        second = self.process(crowd, 2)
        third = self.process(crowd, 3)
        repeated = self.process(crowd, 4)
        self.process([], 4)
        self.process([], 5)
        self.process([], 6)
        self.assertEqual(self.process(crowd, 7), [])
        self.process([], 8)
        self.process([], 9)
        self.process([], 10)
        self.process([], 11)
        self.process([], 12)
        self.assertEqual(self.process(crowd, 13), [])
        self.assertEqual(self.process(crowd, 14), [])
        rearmed = self.process(crowd, 15)

        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertEqual([event["event_type"] for event in third], ["CROWD_THRESHOLD_EXCEEDED"])
        self.assertEqual(repeated, [])
        self.assertEqual([event["event_type"] for event in rearmed], ["CROWD_THRESHOLD_EXCEEDED"])

    def test_crowd_threshold_counts_only_qualified_person_detections(self):
        crowd = [
            {**person((index * 10, 20, index * 10 + 8, 90), confidence=confidence),
             "person_id": f"P-{index}"}
            for index, confidence in enumerate((0.9, 0.89, 0.95))
        ]

        self.assertEqual(self.process(crowd, 1), [])
        crowd[1]["confidence"] = 0.9
        self.assertEqual(self.process(crowd, 2), [])
        self.assertEqual(self.process(crowd, 3), [])
        result = self.process(crowd, 4)

        self.assertEqual(
            [event["event_type"] for event in result],
            ["CROWD_THRESHOLD_EXCEEDED"],
        )
        self.assertIn("90% confidence", result[0]["description"])


if __name__ == "__main__":
    unittest.main()
