import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from backend.config import DATA_DIR, DATABASE_PATH, MIN_INCIDENT_CONFIDENCE


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def database():
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database():
    with database() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                phase TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                error TEXT,
                duration_seconds REAL,
                analyzed_frames INTEGER NOT NULL DEFAULT 0,
                session_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                analysis_id TEXT NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
                frame_index INTEGER NOT NULL,
                timestamp_seconds REAL NOT NULL,
                label TEXT NOT NULL,
                confidence REAL NOT NULL,
                x1 REAL NOT NULL,
                y1 REAL NOT NULL,
                x2 REAL NOT NULL,
                y2 REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS detections_analysis_id
                ON detections(analysis_id);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_name TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                settings TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                ended_at TEXT
            );
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                analysis_id TEXT,
                frame_index INTEGER NOT NULL,
                timestamp_seconds REAL NOT NULL,
                label TEXT NOT NULL,
                confidence REAL NOT NULL,
                x1 REAL NOT NULL,
                y1 REAL NOT NULL,
                x2 REAL NOT NULL,
                y2 REAL NOT NULL,
                person_id TEXT
            );
            CREATE INDEX IF NOT EXISTS observations_session_id
                ON observations(session_id, frame_index);
            CREATE INDEX IF NOT EXISTS observations_analysis_id
                ON observations(analysis_id);
            CREATE TABLE IF NOT EXISTS person_tracks (
                session_id TEXT NOT NULL REFERENCES sessions(id),
                person_id TEXT NOT NULL,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                duration_seconds REAL NOT NULL,
                PRIMARY KEY (session_id, person_id)
            );
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                timestamp_seconds REAL NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                severity TEXT NOT NULL,
                confidence REAL NOT NULL,
                person_id TEXT,
                description TEXT NOT NULL,
                snapshot_path TEXT,
                ai_explanation TEXT NOT NULL,
                recommended_action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'NEW',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                ended_at_seconds REAL,
                last_seen_seconds REAL,
                duration_seconds REAL,
                is_active INTEGER NOT NULL DEFAULT 0,
                summary_generated INTEGER NOT NULL DEFAULT 0,
                security_alerted_at TEXT
            );
            CREATE INDEX IF NOT EXISTS incidents_timeline
                ON incidents(created_at DESC);
            CREATE INDEX IF NOT EXISTS incidents_session
                ON incidents(session_id, timestamp_seconds);
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(analyses)").fetchall()
        }
        if "session_id" not in columns:
            connection.execute("ALTER TABLE analyses ADD COLUMN session_id TEXT")
        incident_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(incidents)").fetchall()
        }
        for name, definition in (
            ("ended_at_seconds", "REAL"),
            ("last_seen_seconds", "REAL"),
            ("duration_seconds", "REAL"),
            ("is_active", "INTEGER NOT NULL DEFAULT 0"),
            ("summary_generated", "INTEGER NOT NULL DEFAULT 0"),
            ("security_alerted_at", "TEXT"),
        ):
            if name not in incident_columns:
                connection.execute(
                    f"ALTER TABLE incidents ADD COLUMN {name} {definition}"
                )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS incidents_one_active
            ON incidents(source_id, event_type, COALESCE(person_id, ''))
            WHERE is_active = 1
            """
        )


initialize_database()


def create_session(source_type, source_name):
    session_id = str(uuid.uuid4())
    with database() as connection:
        connection.execute(
            """
            INSERT INTO sessions (id, source_type, source_name, started_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, source_type, Path(source_name).name, utc_now()),
        )
    return session_id


def get_session(session_id):
    with database() as connection:
        row = connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def update_session(session_id, **fields):
    if not fields:
        return
    assignments = ", ".join(f"{field} = ?" for field in fields)
    with database() as connection:
        connection.execute(
            f"UPDATE sessions SET {assignments} WHERE id = ?",
            (*fields.values(), session_id),
        )


def save_observations(session_id, analysis_id, frame_index, timestamp, detections):
    if not detections:
        return
    with database() as connection:
        connection.executemany(
            """
            INSERT INTO observations (
                session_id, analysis_id, frame_index, timestamp_seconds,
                label, confidence, x1, y1, x2, y2, person_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    session_id,
                    analysis_id,
                    frame_index,
                    timestamp,
                    item["label"],
                    item["confidence"],
                    *item["bbox"],
                    item.get("person_id"),
                )
                for item in detections
            ],
        )
        tracks = {
            item["person_id"]: item["track"]
            for item in detections
            if item.get("person_id") and item.get("track")
        }
        connection.executemany(
            """
            INSERT INTO person_tracks (
                session_id, person_id, first_seen, last_seen, duration_seconds
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id, person_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                duration_seconds = excluded.duration_seconds
            """,
            [
                (
                    session_id,
                    person_id,
                    track["first_seen"],
                    track["last_seen"],
                    track["duration_seconds"],
                )
                for person_id, track in tracks.items()
            ],
        )


def create_incident(
    *,
    event,
    source_type,
    source_id,
    session_id,
    snapshot,
):
    incident_id = str(uuid.uuid4())
    created_at = utc_now()
    snapshot_path = None
    if snapshot is not None:
        snapshot_dir = DATA_DIR / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        destination = snapshot_dir / f"{incident_id}.jpg"
        import cv2

        if not cv2.imwrite(str(destination), snapshot):
            raise OSError(f"Could not save incident snapshot {destination.name}.")
        snapshot_path = str(destination)

    with database() as connection:
        connection.execute(
            """
            INSERT INTO incidents (
                id, event_type, timestamp_seconds, source_type, source_id,
                session_id, severity, confidence, person_id, description,
                snapshot_path, ai_explanation, recommended_action,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW', ?, ?)
            """,
            (
                incident_id,
                event["event_type"],
                event["timestamp_seconds"],
                source_type,
                source_id,
                session_id,
                event["severity"],
                event["confidence"],
                event.get("person_id"),
                event["description"],
                snapshot_path,
                event["ai_explanation"],
                event["recommended_action"],
                created_at,
                created_at,
            ),
        )
    return get_incident(incident_id)


def upsert_active_incident(*, event, source_type, source_id, session_id, snapshot):
    created_at = utc_now()
    event_type = event["event_type"]
    person_id = event.get("person_id")
    created_now = False
    with database() as connection:
        existing = connection.execute(
            """
            SELECT id FROM incidents
            WHERE source_id = ? AND event_type = ?
                AND COALESCE(person_id, '') = COALESCE(?, '') AND is_active = 1
            """,
            (source_id, event_type, person_id),
        ).fetchone()
        if existing:
            connection.execute(
                """
                UPDATE incidents SET last_seen_seconds = ?, duration_seconds = ?,
                    confidence = ?, updated_at = ? WHERE id = ? AND is_active = 1
                """,
                (
                    event.get("last_seen_timestamp_seconds"),
                    event.get("duration_seconds"),
                    event["confidence"],
                    created_at,
                    existing["id"],
                ),
            )
            incident_id = existing["id"]
        else:
            created_now = True
            incident_id = str(uuid.uuid4())
            snapshot_path = None
            if snapshot is not None:
                snapshot_dir = DATA_DIR / "snapshots"
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                destination = snapshot_dir / f"{incident_id}.jpg"
                import cv2

                if not cv2.imwrite(str(destination), snapshot):
                    raise OSError(f"Could not save incident snapshot {destination.name}.")
                snapshot_path = str(destination)
            connection.execute(
                """
                INSERT INTO incidents (
                    id, event_type, timestamp_seconds, source_type, source_id,
                    session_id, severity, confidence, person_id, description,
                    snapshot_path, ai_explanation, recommended_action,
                    status, created_at, updated_at, last_seen_seconds,
                    duration_seconds, is_active, summary_generated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'NEW', ?, ?, ?, ?, 1, 0)
                """,
                (
                    incident_id,
                    event_type,
                    event["started_at_seconds"],
                    source_type,
                    source_id,
                    session_id,
                    event["severity"],
                    event["confidence"],
                    person_id,
                    event["description"],
                    snapshot_path,
                    event["ai_explanation"],
                    event["recommended_action"],
                    created_at,
                    created_at,
                    event.get("last_seen_timestamp_seconds"),
                    event.get("duration_seconds", 0.0),
                ),
            )
    incident = get_incident(incident_id)
    incident["created_now"] = created_now
    return incident


def finish_active_incident(*, event, source_id):
    ended_at = event.get("ended_at_seconds")
    with database() as connection:
        row = connection.execute(
            """
            SELECT id FROM incidents
            WHERE source_id = ? AND event_type = ?
                AND COALESCE(person_id, '') = COALESCE(?, '') AND is_active = 1
            """,
            (source_id, event["event_type"], event.get("person_id")),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            """
            UPDATE incidents SET ended_at_seconds = ?, last_seen_seconds = ?,
                duration_seconds = ?, is_active = 0, updated_at = ?
            WHERE id = ? AND is_active = 1
            """,
            (
                ended_at,
                ended_at,
                event.get("duration_seconds"),
                utc_now(),
                row["id"],
            ),
        )
        incident_id = row["id"]
    return get_incident(incident_id)


def claim_incident_summary(incident_id):
    with database() as connection:
        cursor = connection.execute(
            """
            UPDATE incidents SET summary_generated = 1
            WHERE id = ? AND is_active = 0 AND summary_generated = 0
            """,
            (incident_id,),
        )
    return cursor.rowcount == 1


def update_incident_summary(incident_id, summary):
    with database() as connection:
        connection.execute(
            """
            UPDATE incidents SET ai_explanation = ?, updated_at = ?
            WHERE id = ? AND summary_generated = 1
            """,
            (summary, utc_now(), incident_id),
        )


def get_incident(incident_id):
    with database() as connection:
        row = connection.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
    if not row:
        return None
    incident = dict(row)
    incident["snapshot_url"] = (
        f"/api/incidents/{incident_id}/snapshot"
        if incident["snapshot_path"]
        else None
    )
    return incident


def list_analyses(limit=20, status=None):
    query = (
        "SELECT id, filename, status, phase, message, duration_seconds, "
        "created_at, updated_at, session_id FROM analyses"
    )
    params = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with database() as connection:
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def list_incidents(limit=100, status=None):
    query = "SELECT id FROM incidents WHERE confidence >= ?"
    params = [MIN_INCIDENT_CONFIDENCE]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with database() as connection:
        ids = connection.execute(query, params).fetchall()
    return [get_incident(row["id"]) for row in ids]


def update_incident_status(incident_id, status):
    with database() as connection:
        cursor = connection.execute(
            "UPDATE incidents SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), incident_id),
        )
    return cursor.rowcount > 0


def mark_incident_security_alerted(incident_id):
    with database() as connection:
        cursor = connection.execute(
            """
            UPDATE incidents SET security_alerted_at = ?, updated_at = ?
            WHERE id = ? AND security_alerted_at IS NULL
            """,
            (utc_now(), utc_now(), incident_id),
        )
    return cursor.rowcount > 0


def update_incident_explanation(incident_id, explanation, action):
    with database() as connection:
        connection.execute(
            """
            UPDATE incidents SET ai_explanation = ?, recommended_action = ?,
                updated_at = ? WHERE id = ?
            """,
            (explanation, action, utc_now(), incident_id),
        )
