import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from backend.config import DETECTION_CONFIDENCE_THRESHOLD, MIN_INCIDENT_CONFIDENCE
from backend.database import list_analyses, list_incidents
from backend.main import analysis_response, analytics, incident_assistant


class IncidentConfidenceReportingTests(unittest.TestCase):
    def test_video_summary_filters_scores_and_deduplicates_categories(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.executescript(
            """
            CREATE TABLE analyses (id TEXT, status TEXT);
            CREATE TABLE observations (
                analysis_id TEXT, confidence REAL, label TEXT,
                frame_index INTEGER, timestamp_seconds REAL
            );
            CREATE TABLE incidents (id TEXT, source_id TEXT, confidence REAL, timestamp_seconds REAL);
            INSERT INTO analyses VALUES ('analysis-1', 'completed');
            """
        )
        connection.executemany(
            "INSERT INTO observations VALUES (?, ?, ?, ?, ?)",
            [
                ("analysis-1", 0.70, "chair", 0, 0.0),
                ("analysis-1", 0.80, "chair", 0, 0.0),
                ("analysis-1", 0.90, "chair", 1, 1.0),
                ("analysis-1", DETECTION_CONFIDENCE_THRESHOLD - 0.01, "cup", 0, 0.0),
                ("analysis-1", 0.70, "cup", 1, 1.0),
            ],
        )

        @contextmanager
        def test_database():
            yield connection

        with patch("backend.main.database", test_database):
            result = analysis_response("analysis-1")

        self.assertEqual(result["detection_count"], 4)
        self.assertEqual(
            {item["label"]: item["count"] for item in result["object_counts"]},
            {"chair": 2, "cup": 1},
        )
        self.assertEqual(
            {item["label"]: item["frames_observed"] for item in result["detections"]},
            {"chair": 2, "cup": 1},
        )

    def test_analytics_excludes_incidents_below_confidence_threshold(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.executescript(
            """
            CREATE TABLE person_tracks (person_id TEXT);
            CREATE TABLE incidents (
                id TEXT, event_type TEXT, severity TEXT, status TEXT,
                confidence REAL, created_at TEXT
            );
            CREATE TABLE analyses (status TEXT);
            CREATE TABLE sessions (source_type TEXT, active INTEGER);
            CREATE TABLE observations (label TEXT, confidence REAL);
            INSERT INTO incidents VALUES
                ('high-1', 'ZONE', 'HIGH', 'NEW', 0.95, '2026-01-01'),
                ('low', 'ZONE', 'HIGH', 'NEW', 0.89, '2026-01-02'),
                ('medium-1', 'CROWD', 'MEDIUM', 'RESOLVED', 0.90, '2026-01-03');
            """
        )

        @contextmanager
        def test_database():
            yield connection

        incidents_by_id = {
            "high-1": {"id": "high-1", "confidence": 0.95},
            "medium-1": {"id": "medium-1", "confidence": 0.90},
        }
        with (
            patch("backend.main.database", test_database),
            patch(
                "backend.main.get_incident",
                side_effect=lambda incident_id: incidents_by_id[incident_id],
            ),
        ):
            result = analytics()

        self.assertEqual(result["totals"]["incidents"], 2)
        self.assertEqual(result["totals"]["high_priority_alerts"], 1)
        self.assertEqual(
            {item["name"]: item["value"] for item in result["severity"]},
            {"HIGH": 1, "MEDIUM": 1},
        )
        self.assertEqual(
            {item["name"]: item["value"] for item in result["event_stats"]},
            {"ZONE": 1, "CROWD": 1},
        )
        self.assertEqual(
            {item["id"] for item in result["recent_incidents"]},
            {"high-1", "medium-1"},
        )

    def test_incident_timeline_list_excludes_below_threshold_records(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE incidents (id TEXT, confidence REAL, status TEXT, created_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO incidents VALUES (?, ?, ?, ?)",
            [
                ("qualified", MIN_INCIDENT_CONFIDENCE, "NEW", "2026-01-01"),
                ("unqualified", MIN_INCIDENT_CONFIDENCE - 0.01, "NEW", "2026-01-02"),
            ],
        )

        @contextmanager
        def test_database():
            yield connection

        with (
            patch("backend.database.database", test_database),
            patch(
                "backend.database.get_incident",
                side_effect=lambda incident_id: {"id": incident_id},
            ),
        ):
            result = list_incidents()

        self.assertEqual([incident["id"] for incident in result], ["qualified"])

    def test_saved_analysis_list_returns_recent_completed_records(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.executescript(
            """
            CREATE TABLE analyses (
                id TEXT, filename TEXT, status TEXT, progress REAL,
                phase TEXT, message TEXT, duration_seconds REAL,
                created_at TEXT, updated_at TEXT, session_id TEXT
            );
            INSERT INTO analyses VALUES
                ('a2', 'latest.mp4', 'completed', 100, 'Complete', 'Done', 15.5, '2026-01-03T00:00:00Z', '2026-01-03T00:00:00Z', 's2'),
                ('a1', 'older.mp4', 'completed', 100, 'Complete', 'Done', 10.0, '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z', 's1'),
                ('a3', 'queued.mp4', 'queued', 20, 'Queued', 'Waiting', 0, '2026-01-04T00:00:00Z', '2026-01-04T00:00:00Z', 's3');
            """
        )

        @contextmanager
        def test_database():
            yield connection

        with patch("backend.database.database", test_database):
            result = list_analyses(limit=10, status="completed")

        self.assertEqual([item["id"] for item in result], ["a2", "a1"])

    def test_incident_assistant_summarizes_saved_incidents_from_metadata(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.executescript(
            """
            CREATE TABLE incidents (
                id TEXT, event_type TEXT, severity TEXT, status TEXT,
                confidence REAL, created_at TEXT, description TEXT
            );
            INSERT INTO incidents VALUES
                ('i1', 'RESTRICTED_ZONE_ENTRY', 'HIGH', 'NEW', 0.96, '2026-01-01T00:00:00Z', 'Person entered restricted area'),
                ('i2', 'RESTRICTED_ZONE_ENTRY', 'HIGH', 'REVIEWED', 0.93, '2026-01-01T01:00:00Z', 'Person entered restricted area again'),
                ('i3', 'CROWD_THRESHOLD_EXCEEDED', 'MEDIUM', 'NEW', 0.94, '2026-01-02T00:00:00Z', 'Crowd threshold exceeded');
            """
        )

        @contextmanager
        def test_database():
            yield connection

        with patch("backend.main.database", test_database):
            result = incident_assistant("What events happened recently?")

        self.assertIn("RESTRICTED_ZONE_ENTRY", result["answer"])
        self.assertIn("2", result["answer"])
        self.assertIn("CROWD_THRESHOLD_EXCEEDED", result["answer"])


if __name__ == "__main__":
    unittest.main()
