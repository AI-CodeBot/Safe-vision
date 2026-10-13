import asyncio
import csv
import io
import json
import hmac
import logging
import math
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backend.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE,
    configured_password,
    create_session_token,
    is_valid_session_token,
    validate_auth_configuration,
)
from backend.config import (
    ALLOWED_EXTENSIONS,
    ANALYSES_DIR,
    DATA_DIR,
    DETECTION_CONFIDENCE_THRESHOLD,
    MAX_SAMPLE_INTERVAL,
    MAX_UPLOAD_BYTES,
    MIN_INCIDENT_CONFIDENCE,
    ROOT,
)
from backend.database import (
    create_session,
    database,
    claim_incident_summary,
    finish_active_incident,
    get_incident,
    get_session,
    initialize_database,
    list_analyses,
    list_incidents,
    mark_incident_security_alerted,
    save_observations,
    update_incident_summary,
    update_incident_status,
    upsert_active_incident,
    update_session,
    utc_now,
)
from backend.detector import (
    DetectorUnavailable,
    annotate_frame,
    detect_frame,
    detector_status,
    get_model,
)
from backend.events import (
    DEFAULT_CONFIRMATION_SAMPLES,
    DEFAULT_INCIDENT_RULES,
    INCIDENT_GRACE_SECONDS,
    IncidentEngine,
)
from backend.gemini import describe_live_frame, summarize_incident
from backend.notifications import send_security_alert
from backend.tracker import PersonTracker


logger = logging.getLogger("safevision")
validate_auth_configuration()
app = FastAPI(title="SafeVision Video Analysis", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "SAFEVISION_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)
inference_lock = threading.Lock()
session_engines = {}
session_trackers = {}
session_locks = {}
state_lock = threading.Lock()
gemini_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gemini")
MAX_LIVE_FRAME_BYTES = 5 * 1024 * 1024
VISUAL_SEARCH_TERMS = (
    "red shirt",
    "red-shirt",
    "redshirt",
    "blue shirt",
    "shirt color",
    "wearing",
    "clothing",
)

initialize_database()


class LoginRequest(BaseModel):
    password: str


@app.middleware("http")
async def require_authentication(request, call_next):
    password = configured_password()
    if password is None:
        return await call_next(request)

    if request.method == "OPTIONS" or request.url.path in {
        "/login",
        "/health",
        "/api/health",
        "/api/auth/login",
    }:
        return await call_next(request)

    if is_valid_session_token(
        request.cookies.get(SESSION_COOKIE_NAME), password
    ):
        return await call_next(request)

    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=401,
            content={"detail": "Sign in to access SafeVision."},
        )
    return RedirectResponse(url="/login", status_code=303)


@app.post("/api/auth/login")
def login(payload: LoginRequest):
    expected_password = configured_password()
    if expected_password is None:
        raise HTTPException(status_code=503, detail="Authentication is not configured.")
    if not hmac.compare_digest(payload.password, expected_password):
        raise HTTPException(status_code=401, detail="Incorrect access password.")

    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session_token(expected_password),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=os.getenv("SAFEVISION_ENV", "development").lower() == "production",
        samesite="strict",
        path="/",
    )
    return response


@app.post("/api/auth/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite="strict")
    return response


def update_analysis(analysis_id, **values):
    values["updated_at"] = utc_now()
    assignments = ", ".join(f"{column} = ?" for column in values)
    with database() as connection:
        connection.execute(
            f"UPDATE analyses SET {assignments} WHERE id = ?",
            (*values.values(), analysis_id),
        )


def session_runtime(session_id):
    with state_lock:
        if session_id not in session_engines:
            session_engines[session_id] = IncidentEngine()
            session_trackers[session_id] = PersonTracker(
                max_gap_seconds=INCIDENT_GRACE_SECONDS
            )
            session_locks[session_id] = threading.Lock()
        return (
            session_engines[session_id],
            session_trackers[session_id],
            session_locks[session_id],
        )


def validate_settings(settings):
    if not isinstance(settings, dict):
        raise HTTPException(status_code=422, detail="Settings must be a JSON object.")
    zone = settings.get("restricted_zone")
    if zone is not None:
        if not isinstance(zone, dict) or not all(
            isinstance(zone.get(key), (int, float))
            and not isinstance(zone.get(key), bool)
            and math.isfinite(zone[key])
            for key in ("x", "y", "width", "height")
        ):
            raise HTTPException(
                status_code=422,
                detail="Restricted zone needs finite x, y, width, and height values.",
            )
        if (
            zone["width"] <= 0
            or zone["height"] <= 0
            or zone["x"] < 0
            or zone["y"] < 0
            or zone["x"] + zone["width"] > 1
            or zone["y"] + zone["height"] > 1
        ):
            raise HTTPException(
                status_code=422,
                detail="Restricted zone must fit inside the video frame.",
            )
    prolonged = settings.get("prolonged_seconds", 15)
    crowd = settings.get("crowd_threshold", 10)
    confidence = settings.get("incident_confidence", MIN_INCIDENT_CONFIDENCE)
    confirmation_samples = settings.get(
        "confirmation_samples", DEFAULT_CONFIRMATION_SAMPLES
    )
    incident_rules = settings.get("incident_rules", DEFAULT_INCIDENT_RULES)
    if (
        isinstance(prolonged, bool)
        or not isinstance(prolonged, (int, float))
        or not 1 <= prolonged <= 3600
    ):
        raise HTTPException(
            status_code=422, detail="Prolonged-presence threshold must be 1–3600 seconds."
        )
    if isinstance(crowd, bool) or not isinstance(crowd, int) or not 2 <= crowd <= 500:
        raise HTTPException(
            status_code=422, detail="Crowd threshold must be a whole number from 2 to 500."
        )
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not MIN_INCIDENT_CONFIDENCE <= confidence <= 0.99
    ):
        raise HTTPException(
            status_code=422,
            detail="Incident confidence must be between 0.90 and 0.99.",
        )
    if (
        isinstance(confirmation_samples, bool)
        or not isinstance(confirmation_samples, int)
        or not 3 <= confirmation_samples <= 10
    ):
        raise HTTPException(
            status_code=422,
            detail="Confirmation samples must be a whole number from 3 to 10.",
        )
    if not isinstance(incident_rules, dict) or any(
        not isinstance(incident_rules.get(rule), bool)
        for rule in DEFAULT_INCIDENT_RULES
    ):
        raise HTTPException(
            status_code=422,
            detail="Incident rules must provide boolean values for each incident type.",
        )
    return {
        "restricted_zone": zone,
        "prolonged_seconds": float(prolonged),
        "crowd_threshold": crowd,
        "incident_confidence": float(confidence),
        "confirmation_samples": confirmation_samples,
        "incident_rules": {
            rule: incident_rules[rule] for rule in DEFAULT_INCIDENT_RULES
        },
    }


def enrich_incident(incident_id, event, snapshot_path):
    try:
        summary = summarize_incident(event, snapshot_path)
        update_incident_summary(incident_id, summary)
    except Exception:
        logger.exception("Could not summarize incident %s with Gemini", incident_id)


def public_incident(incident):
    if incident is None:
        return None
    return {key: value for key, value in incident.items() if key != "snapshot_path"}


def emit_incidents(
    events, frame, *, source_type, source_id, session_id
):
    incidents = []
    for event in events:
        incident = upsert_active_incident(
            event=event,
            source_type=source_type,
            source_id=source_id,
            session_id=session_id,
            snapshot=frame.copy() if frame is not None else None,
        )
        created_now = incident.pop("created_now", False)
        result = public_incident(incident)
        result["is_new"] = created_now
        incidents.append(result)
    return incidents


def finish_incidents(events, *, source_id):
    for event in events:
        incident = finish_active_incident(event=event, source_id=source_id)
        if incident and claim_incident_summary(incident["id"]):
            summary_event = {
                **event,
                "started_at_seconds": incident["timestamp_seconds"],
                "ended_at_seconds": incident["ended_at_seconds"],
                "duration_seconds": incident["duration_seconds"],
            }
            gemini_executor.submit(
                enrich_incident,
                incident["id"],
                summary_event,
                incident["snapshot_path"],
            )


def process_frame(frame, session_id, settings, timestamp, frame_index, analysis_id=None):
    height, width = frame.shape[:2]
    engine, tracker, session_lock = session_runtime(session_id)
    with session_lock:
        session = get_session(session_id)
        if not session or not session["active"]:
            raise HTTPException(status_code=404, detail="Active camera session not found.")
        with inference_lock:
            detections = detect_frame(frame)
        detections = tracker.update(detections, timestamp)
        save_observations(
            session_id, analysis_id, frame_index, timestamp, detections
        )
        engine.update(
            detections,
            timestamp=timestamp,
            width=width,
            height=height,
            zone=settings.get("restricted_zone"),
            prolonged_seconds=settings.get("prolonged_seconds", 15),
            crowd_threshold=settings.get("crowd_threshold", 10),
            confidence_threshold=settings.get(
                "incident_confidence", MIN_INCIDENT_CONFIDENCE
            ),
            confirmation_samples=settings.get(
                "confirmation_samples", DEFAULT_CONFIRMATION_SAMPLES
            ),
            incident_rules=settings.get("incident_rules", DEFAULT_INCIDENT_RULES),
        )
        annotated = annotate_frame(frame.copy(), detections)
        finish_incidents(engine.drain_ended_events(), source_id=session_id)
        incidents = emit_incidents(
            engine.active_events(),
            annotated,
            source_type="video" if analysis_id else "camera",
            source_id=analysis_id or session_id,
            session_id=session_id,
        )
    return detections, annotated, incidents


def analysis_response(analysis_id):
    with database() as connection:
        row = connection.execute(
            "SELECT * FROM analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Analysis not found.")
        result = dict(row)
        result["video_url"] = (
            f"/api/analyses/{analysis_id}/video"
            if row["status"] == "completed"
            else None
        )
        result["detection_count"] = connection.execute(
            """
            SELECT COUNT(*) FROM observations
            WHERE analysis_id = ? AND confidence >= ?
            """,
            (analysis_id, DETECTION_CONFIDENCE_THRESHOLD),
        ).fetchone()[0]
        result["object_counts"] = [
            dict(item)
            for item in connection.execute(
                """
                SELECT label, MAX(frame_count) AS count FROM (
                    SELECT label, frame_index, COUNT(*) AS frame_count
                    FROM observations
                    WHERE analysis_id = ? AND confidence >= ?
                    GROUP BY label, frame_index
                )
                GROUP BY label ORDER BY count DESC, label
                """,
                (analysis_id, DETECTION_CONFIDENCE_THRESHOLD),
            ).fetchall()
        ]
        result["detections"] = [
            dict(item)
            for item in connection.execute(
                """
                SELECT label, MAX(confidence) AS confidence,
                       MIN(timestamp_seconds) AS timestamp_seconds,
                       COUNT(DISTINCT frame_index) AS frames_observed
                FROM observations
                WHERE analysis_id = ? AND confidence >= ?
                GROUP BY label ORDER BY confidence DESC, label LIMIT 200
                """,
                (analysis_id, DETECTION_CONFIDENCE_THRESHOLD),
            ).fetchall()
        ]
        result["incidents"] = [
            dict(item)
            for item in connection.execute(
                """
                SELECT id FROM incidents
                WHERE source_id = ? AND confidence >= ?
                ORDER BY timestamp_seconds
                """,
                (analysis_id, MIN_INCIDENT_CONFIDENCE),
            ).fetchall()
        ]
    for incident in result["incidents"]:
        persisted = get_incident(incident["id"])
        incident.update(public_incident(persisted) or {})
    return result


def run_video_analysis(analysis_id, session_id, input_path, output_path, sample_interval):
    capture = cv2.VideoCapture(str(input_path))
    writer = None
    engine = None
    with database() as connection:
        session = connection.execute(
            "SELECT settings FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    try:
        update_analysis(
            analysis_id,
            status="running",
            phase="Opening video",
            message="Opening footage and loading the object detector.",
        )
        if not capture.isOpened():
            raise ValueError("OpenCV could not open the uploaded video.")
        fps = capture.get(cv2.CAP_PROP_FPS)
        frame_count = max(0, int(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if not math.isfinite(fps) or fps <= 0 or width <= 0 or height <= 0:
            raise ValueError("The uploaded video has invalid frame metadata.")
        duration = frame_count / fps if frame_count else 0
        frame_stride = max(1, round(fps * sample_interval))
        writer = cv2.VideoWriter(
            str(output_path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            fps,
            (width, height),
        )
        if not writer.isOpened():
            raise RuntimeError("OpenCV could not create the annotated MP4 video.")
        try:
            get_model()
        except DetectorUnavailable as error:
            raise RuntimeError(str(error)) from error

        settings = json.loads(session["settings"]) if session else {}
        settings = validate_settings(settings)
        grace_seconds = max(INCIDENT_GRACE_SECONDS, sample_interval * 2)
        tracker = PersonTracker(max_gap_seconds=grace_seconds)
        engine = IncidentEngine()
        frame_index = 0
        analyzed_frames = 0
        last_progress_update = 0
        current_detections = []
        while True:
            success, frame = capture.read()
            if not success:
                break
            if frame_index % frame_stride == 0:
                with inference_lock:
                    current_detections = detect_frame(frame)
                timestamp = frame_index / fps
                current_detections = tracker.update(current_detections, timestamp)
                save_observations(
                    session_id,
                    analysis_id,
                    frame_index,
                    timestamp,
                    current_detections,
                )
                engine.update(
                    current_detections,
                    timestamp=timestamp,
                    width=width,
                    height=height,
                    zone=settings["restricted_zone"],
                    prolonged_seconds=settings["prolonged_seconds"],
                    crowd_threshold=settings["crowd_threshold"],
                    confidence_threshold=settings["incident_confidence"],
                    confirmation_samples=settings["confirmation_samples"],
                    grace_seconds=grace_seconds,
                    incident_rules=settings["incident_rules"],
                )
                analyzed_frames += 1

            annotated = annotate_frame(frame, current_detections)
            writer.write(annotated)
            if frame_index % frame_stride == 0:
                finish_incidents(
                    engine.drain_ended_events(), source_id=analysis_id
                )
                emit_incidents(
                    engine.active_events(),
                    annotated,
                    source_type="video",
                    source_id=analysis_id,
                    session_id=session_id,
                )

            frame_index += 1
            if frame_index - last_progress_update >= max(1, int(fps)):
                progress = (
                    min(99, frame_index / frame_count * 100)
                    if frame_count
                    else 0
                )
                update_analysis(
                    analysis_id,
                    progress=progress,
                    analyzed_frames=analyzed_frames,
                    message=f"Processed {frame_index} of {frame_count or 'unknown'} frames.",
                )
                last_progress_update = frame_index

        if frame_index == 0:
            raise ValueError("The uploaded video does not contain readable frames.")
        capture.release()
        writer.release()
        writer = None
        finish_incidents(engine.finalize(), source_id=analysis_id)
        update_analysis(
            analysis_id,
            status="completed",
            progress=100,
            phase="Complete",
            message="Detection results, incidents, and annotated video are ready.",
            duration_seconds=duration or frame_index / fps,
            analyzed_frames=analyzed_frames,
        )
        update_session(session_id, active=0, ended_at=utc_now())
    except Exception as error:
        logger.exception("Video analysis %s failed", analysis_id)
        update_analysis(
            analysis_id,
            status="failed",
            phase="Failed",
            message="Video processing failed.",
            error=str(error),
        )
        update_session(session_id, active=0, ended_at=utc_now())
    finally:
        if engine is not None:
            finish_incidents(engine.finalize(), source_id=analysis_id)
        capture.release()
        if writer is not None:
            writer.release()


@app.get("/health")
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        **detector_status(),
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "incident_confidence_threshold": MIN_INCIDENT_CONFIDENCE,
        "detection_confidence_threshold": DETECTION_CONFIDENCE_THRESHOLD,
        "incident_confirmation_samples": DEFAULT_CONFIRMATION_SAMPLES,
        "gemini_enabled": bool(os.getenv("GEMINI_API_KEY")),
    }


@app.post("/api/sessions", status_code=201)
@app.post("/api/start-session", status_code=201)
def start_session(payload: dict):
    source_name = str(payload.get("source_name") or "Live camera")
    settings = validate_settings(payload.get("settings", {}))
    session_id = create_session("camera", source_name)
    update_session(session_id, settings=json.dumps(settings))
    session_runtime(session_id)
    return {"id": session_id, "source_type": "camera", "settings": settings}


@app.put("/api/sessions/{session_id}/settings")
def save_session_settings(session_id: str, payload: dict):
    session = get_session(session_id)
    if not session or not session["active"]:
        raise HTTPException(status_code=404, detail="Active camera session not found.")
    settings = validate_settings(payload)
    update_session(session_id, settings=json.dumps(settings))
    return {"settings": settings}


def _analyze_frame(session_id, file, timestamp_seconds, settings):
    session = get_session(session_id)
    if not session or not session["active"]:
        raise HTTPException(status_code=404, detail="Active camera session not found.")
    try:
        frame_bytes = file.file.read(4 * 1024 * 1024 + 1)
        if not frame_bytes or len(frame_bytes) > 4 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Frame must be under 4 MB.")
        encoded = np.frombuffer(frame_bytes, dtype=np.uint8)
        frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if frame is None:
            raise HTTPException(status_code=415, detail="Could not decode the camera frame.")
        detections, _, incidents = process_frame(
            frame,
            session_id,
            validate_settings(settings),
            timestamp_seconds,
            round(timestamp_seconds * 2),
        )
    except DetectorUnavailable as error:
        raise HTTPException(
            status_code=503,
            detail=f"AI detection is unavailable. Camera preview can continue: {error}",
        ) from error
    return {
        "width": int(frame.shape[1]),
        "height": int(frame.shape[0]),
        "detections": detections,
        "people_count": sum(item["label"] == "person" for item in detections),
        "objects_count": len(detections),
        "incidents": incidents,
    }


@app.post("/api/sessions/{session_id}/frame")
async def analyze_session_frame(
    session_id: str,
    file: UploadFile = File(...),
    timestamp_seconds: float = Form(...),
    settings: str = Form("{}"),
):
    if not math.isfinite(timestamp_seconds) or timestamp_seconds < 0:
        raise HTTPException(status_code=422, detail="Frame timestamp must be non-negative.")
    try:
        parsed_settings = json.loads(settings)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=422, detail="Settings must be valid JSON.") from error
    return _analyze_frame(session_id, file, timestamp_seconds, parsed_settings)


@app.post("/api/analyze-frame")
async def analyze_frame_endpoint(
    session_id: str = Form(...),
    file: UploadFile = File(...),
    timestamp_seconds: float = Form(...),
    settings: str = Form("{}"),
):
    return await analyze_session_frame(session_id, file, timestamp_seconds, settings)


@app.post("/api/sessions/{session_id}/stop")
@app.post("/api/stop-session/{session_id}")
def stop_session(session_id: str):
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    with state_lock:
        engine = session_engines.get(session_id)
        session_lock = session_locks.get(session_id)
    if session_lock:
        with session_lock:
            update_session(session_id, active=0, ended_at=utc_now())
            ended_events = engine.finalize() if engine is not None else []
    else:
        update_session(session_id, active=0, ended_at=utc_now())
        ended_events = []
    finish_incidents(ended_events, source_id=session_id)
    with state_lock:
        if session_engines.get(session_id) is engine:
            session_engines.pop(session_id, None)
            session_trackers.pop(session_id, None)
            session_locks.pop(session_id, None)
    return {"id": session_id, "active": False}


@app.post("/api/analyses", status_code=202)
async def create_analysis(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    sample_interval: float = Form(1.0),
    settings: str = Form("{}"),
):
    if not file.filename or Path(file.filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415, detail="Choose an MP4, MOV, AVI, MKV, or WebM video."
        )
    if not math.isfinite(sample_interval) or not 0.1 <= sample_interval <= MAX_SAMPLE_INTERVAL:
        raise HTTPException(
            status_code=422,
            detail=f"Frame sampling interval must be between 0.1 and {MAX_SAMPLE_INTERVAL:g} seconds.",
        )
    try:
        parsed_settings = validate_settings(json.loads(settings))
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=422, detail="Settings must be valid JSON.") from error

    analysis_id = str(uuid.uuid4())
    filename = Path(file.filename).name
    session_id = create_session("video", filename)
    update_session(session_id, settings=json.dumps(parsed_settings))
    analysis_dir = ANALYSES_DIR / analysis_id
    analysis_dir.mkdir()
    input_path = analysis_dir / f"input{Path(filename).suffix.lower()}"
    output_path = analysis_dir / "annotated.mp4"
    size = 0
    try:
        with input_path.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413, detail="Video exceeds the 500 MB upload limit."
                    )
                destination.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="The uploaded video is empty.")
    except Exception:
        input_path.unlink(missing_ok=True)
        analysis_dir.rmdir()
        update_session(session_id, active=0, ended_at=utc_now())
        raise
    finally:
        await file.close()

    capture = cv2.VideoCapture(str(input_path))
    valid_video = capture.isOpened() and capture.read()[0]
    capture.release()
    if not valid_video:
        input_path.unlink(missing_ok=True)
        analysis_dir.rmdir()
        update_session(session_id, active=0, ended_at=utc_now())
        raise HTTPException(status_code=415, detail="OpenCV could not read this video file.")

    timestamp = utc_now()
    with database() as connection:
        connection.execute(
            """
            INSERT INTO analyses (
                id, filename, status, phase, message, session_id, created_at, updated_at
            ) VALUES (?, ?, 'queued', 'Queued', 'Waiting for a processing worker.', ?, ?, ?)
            """,
            (analysis_id, filename, session_id, timestamp, timestamp),
        )
    background_tasks.add_task(
        run_video_analysis,
        analysis_id,
        session_id,
        input_path,
        output_path,
        sample_interval,
    )
    return {"id": analysis_id, "session_id": session_id, "status": "queued"}


@app.get("/api/analyses/{analysis_id}")
def get_analysis(analysis_id: str):
    return analysis_response(analysis_id)


@app.get("/api/analyses/{analysis_id}/video")
def get_annotated_video(analysis_id: str):
    with database() as connection:
        row = connection.execute(
            "SELECT status FROM analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    if row["status"] != "completed":
        raise HTTPException(status_code=409, detail="Annotated video is not ready.")
    video_path = ANALYSES_DIR / analysis_id / "annotated.mp4"
    if not video_path.is_file():
        raise HTTPException(status_code=404, detail="Annotated video file is missing.")
    return FileResponse(
        video_path,
        media_type="video/mp4",
        filename=video_path.name,
        content_disposition_type="inline",
    )


@app.get("/api/incidents")
def incidents(limit: int = 100, status: str | None = None):
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="Incident limit must be 1–500.")
    if status and status not in {"NEW", "REVIEWED", "RESOLVED"}:
        raise HTTPException(status_code=422, detail="Invalid incident status.")
    return {
        "incidents": [
            public_incident(incident)
            for incident in list_incidents(limit, status)
        ]
    }


@app.get("/api/incidents/{incident_id}")
def incident_detail(incident_id: str):
    incident = get_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")
    return public_incident(incident)


@app.get("/api/incidents/{incident_id}/snapshot")
def incident_snapshot(incident_id: str):
    incident = get_incident(incident_id)
    if not incident or not incident["snapshot_path"]:
        raise HTTPException(status_code=404, detail="Incident snapshot not found.")
    snapshot = Path(incident["snapshot_path"]).resolve()
    if DATA_DIR not in snapshot.parents or not snapshot.is_file():
        raise HTTPException(status_code=404, detail="Incident snapshot not found.")
    return FileResponse(snapshot, media_type="image/jpeg")


@app.post("/api/incidents/{incident_id}/review")
def review_incident(incident_id: str):
    if not update_incident_status(incident_id, "REVIEWED"):
        raise HTTPException(status_code=404, detail="Incident not found.")
    return public_incident(get_incident(incident_id))


@app.post("/api/incidents/{incident_id}/resolve")
def resolve_incident(incident_id: str):
    if not update_incident_status(incident_id, "RESOLVED"):
        raise HTTPException(status_code=404, detail="Incident not found.")
    return public_incident(get_incident(incident_id))


@app.post("/api/incidents/{incident_id}/alert-security")
def alert_security(incident_id: str):
    incident = get_incident(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found.")
    if incident["security_alerted_at"]:
        return public_incident(incident)
    try:
        send_security_alert(incident)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    mark_incident_security_alerted(incident_id)
    return public_incident(get_incident(incident_id))


@app.get("/api/analyses")
def list_saved_analyses(limit: int = 20, status: str | None = None):
    if not 1 <= limit <= 200:
        raise HTTPException(status_code=422, detail="Analysis limit must be 1–200.")
    if status and status not in {"queued", "running", "completed", "failed"}:
        raise HTTPException(status_code=422, detail="Invalid analysis status.")
    return {"analyses": list_analyses(limit=limit, status=status)}


@app.get("/api/analytics")
def analytics():
    with database() as connection:
        totals = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM person_tracks) AS people_detected,
                (SELECT COUNT(*) FROM incidents
                    WHERE confidence >= ?) AS incidents,
                (SELECT COUNT(*) FROM incidents WHERE severity = 'HIGH'
                    AND status != 'RESOLVED'
                    AND confidence >= ?) AS high_priority_alerts,
                (SELECT COUNT(*) FROM analyses WHERE status = 'completed')
                    AS videos_analyzed,
                (SELECT COUNT(*) FROM sessions WHERE source_type = 'camera'
                    AND active = 1) AS active_cameras,
                (SELECT COUNT(*) FROM observations
                    WHERE confidence >= ?) AS objects_detected
            """,
            (
                MIN_INCIDENT_CONFIDENCE,
                MIN_INCIDENT_CONFIDENCE,
                DETECTION_CONFIDENCE_THRESHOLD,
            ),
        ).fetchone()
        severity = [
            dict(row)
            for row in connection.execute(
                """
                SELECT severity AS name, COUNT(*) AS value
                FROM incidents WHERE confidence >= ?
                GROUP BY severity ORDER BY severity
                """,
                (MIN_INCIDENT_CONFIDENCE,),
            ).fetchall()
        ]
        event_stats = [
            dict(row)
            for row in connection.execute(
                """
                SELECT event_type AS name, COUNT(*) AS value
                FROM incidents WHERE confidence >= ?
                GROUP BY event_type ORDER BY value DESC
                """,
                (MIN_INCIDENT_CONFIDENCE,),
            ).fetchall()
        ]
        objects = [
            dict(row)
            for row in connection.execute(
                """
                SELECT label AS name, COUNT(*) AS value FROM observations
                GROUP BY label ORDER BY value DESC LIMIT 8
                """
            ).fetchall()
        ]
        recent = connection.execute(
            """
            SELECT id FROM incidents WHERE confidence >= ?
            ORDER BY created_at DESC LIMIT 8
            """,
            (MIN_INCIDENT_CONFIDENCE,),
        ).fetchall()
    return {
        "totals": dict(totals),
        "severity": severity,
        "event_stats": event_stats,
        "objects": objects,
        "recent_incidents": [
            public_incident(get_incident(row["id"])) for row in recent
        ],
    }


@app.get("/api/analytics/csv")
def analytics_csv():
    with database() as connection:
        rows = connection.execute(
            """
                 SELECT id, event_type, severity, status, confidence, person_id,
                     source_type, source_id, timestamp_seconds, ended_at_seconds,
                     duration_seconds, description, created_at
            FROM incidents WHERE confidence >= ?
            ORDER BY created_at DESC
            """,
            (MIN_INCIDENT_CONFIDENCE,),
        ).fetchall()
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "id",
            "event_type",
            "severity",
            "status",
            "confidence",
            "person_id",
            "source_type",
            "source_id",
            "timestamp_seconds",
            "ended_at_seconds",
            "duration_seconds",
            "description",
            "created_at",
        ],
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row[key] for key in writer.fieldnames})
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=safevision-incidents.csv"},
    )


def incident_assistant(question: str):
    if not isinstance(question, str) or not question.strip():
        raise HTTPException(status_code=422, detail="Ask a question about saved incidents.")
    normalized = question.strip().lower()
    with database() as connection:
        rows = connection.execute(
            """
            SELECT event_type, severity, status, confidence, created_at, description
            FROM incidents WHERE confidence >= ?
            ORDER BY created_at DESC LIMIT 20
            """,
            (MIN_INCIDENT_CONFIDENCE,),
        ).fetchall()
    if not rows:
        return {
            "answer": "No saved incidents were found in the local database yet.",
            "counts": {},
            "recent": [],
        }

    counts = {}
    for row in rows:
        counts[row["event_type"]] = counts.get(row["event_type"], 0) + 1
    recent = [dict(row) for row in rows[:5]]

    if "count" in normalized or "how many" in normalized:
        total = len(rows)
        top = ", ".join(f"{name}: {value}" for name, value in sorted(counts.items(), key=lambda item: (-item[1], item[0])))
        answer = f"There are {total} saved incidents above the confidence threshold. The distribution is: {top}."
    elif "zone" in normalized or "restricted" in normalized:
        zone_count = counts.get("RESTRICTED_ZONE_ENTRY", 0)
        answer = f"Restricted-zone events account for {zone_count} saved incidents above the confidence threshold."
    elif "crowd" in normalized:
        crowd_count = counts.get("CROWD_THRESHOLD_EXCEEDED", 0)
        answer = f"Crowd alerts account for {crowd_count} saved incidents above the confidence threshold."
    elif "recent" in normalized or "latest" in normalized or "what happened" in normalized or "what events" in normalized:
        latest = ", ".join(f"{row['event_type']} ({row['severity']})" for row in rows[:4])
        restricted = counts.get("RESTRICTED_ZONE_ENTRY", 0)
        crowd = counts.get("CROWD_THRESHOLD_EXCEEDED", 0)
        answer = (
            f"Recent saved incidents include {latest}. "
            f"There are {restricted} restricted-zone events and {crowd} crowd alerts in the saved dataset."
        )
    else:
        top_event, top_count = max(counts.items(), key=lambda item: item[1])
        answer = (
            f"The most common saved event type is {top_event} with {top_count} incidents. "
            f"Total qualifying incidents: {len(rows)}."
        )

    return {"answer": answer, "counts": counts, "recent": recent}


@app.post("/api/incidents/assistant")
def incident_assistant_route(payload: dict):
    question = payload.get("question") if isinstance(payload, dict) else None
    return incident_assistant(question)


@app.post("/api/live/assistant")
async def live_camera_assistant(
    question: str = Form(...),
    file: UploadFile = File(...),
):
    if not os.getenv("GEMINI_API_KEY"):
        raise HTTPException(status_code=503, detail="Gemini is not configured.")
    if not question.strip() or len(question) > 500:
        raise HTTPException(
            status_code=422,
            detail="Ask a question using 1 to 500 characters.",
        )
    if file.content_type != "image/jpeg":
        raise HTTPException(status_code=415, detail="Upload a JPEG camera frame.")

    image_bytes = await file.read(MAX_LIVE_FRAME_BYTES + 1)
    if not image_bytes:
        raise HTTPException(status_code=422, detail="The camera frame is empty.")
    if len(image_bytes) > MAX_LIVE_FRAME_BYTES:
        raise HTTPException(status_code=413, detail="The camera frame exceeds 5 MB.")
    frame = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(status_code=422, detail="The camera frame is not a valid image.")
    height, width = frame.shape[:2]
    if width > 4096 or height > 4096 or width * height > 12_000_000:
        raise HTTPException(status_code=413, detail="The camera frame dimensions are too large.")
    encoded, jpeg = cv2.imencode(".jpg", frame)
    if not encoded:
        raise HTTPException(status_code=422, detail="The camera frame could not be processed.")

    try:
        answer = await asyncio.get_running_loop().run_in_executor(
            gemini_executor,
            describe_live_frame,
            jpeg.tobytes(),
            question,
        )
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return {"answer": answer, "source": "live_camera"}


@app.post("/api/search")
def search(payload: dict):
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=422, detail="Enter a search query.")
    normalized = query.strip().lower()
    if any(term in normalized for term in VISUAL_SEARCH_TERMS):
        return {
            "query": query,
            "results": [],
            "message": (
                "The stored detections do not include clothing-color attributes. "
                "Visual attribute search needs a separate vision model; no result was inferred."
            ),
            "needs_visual_analysis": True,
        }

    event_type = None
    if "restricted" in normalized or "zone entr" in normalized:
        event_type = "RESTRICTED_ZONE_ENTRY"
    elif "prolonged" in normalized or "remained" in normalized:
        event_type = "PROLONGED_PRESENCE"
    elif "crowd" in normalized or "highest" in normalized:
        event_type = "CROWD_THRESHOLD_EXCEEDED"

    person_match = re.search(r"person\s*#?\s*(\d+)", normalized)
    person_id = f"P-{int(person_match.group(1)):03d}" if person_match else None
    results = []
    if "highest" in normalized and "crowd" in normalized:
        with database() as connection:
            rows = connection.execute(
                """
                SELECT session_id, analysis_id, timestamp_seconds, frame_index,
                       COUNT(*) AS people_count,
                       MAX(person_id) AS person_id
                FROM observations WHERE label = 'person'
                GROUP BY session_id, frame_index
                ORDER BY people_count DESC, timestamp_seconds LIMIT 20
                """
            ).fetchall()
        for row in rows:
            results.append(
                {
                    "source_id": row["analysis_id"] or row["session_id"],
                    "session_id": row["session_id"],
                    "source_type": "video" if row["analysis_id"] else "camera",
                    "timestamp_seconds": row["timestamp_seconds"],
                    "title": "Highest detected crowd",
                    "event_type": "CROWD_COUNT",
                    "person_id": None,
                    "description": f"{row['people_count']} people were detected in this sampled frame.",
                    "people_count": row["people_count"],
                }
            )
    elif event_type:
        with database() as connection:
            ids = connection.execute(
                """
                SELECT id FROM incidents WHERE event_type = ?
                  AND confidence >= ?
                ORDER BY created_at DESC LIMIT 100
                """,
                (event_type, MIN_INCIDENT_CONFIDENCE),
            ).fetchall()
        results = []
        for row in ids:
            incident = get_incident(row["id"])
            if incident and (not person_id or incident["person_id"] == person_id):
                results.append(public_incident(incident))
    else:
        label = "backpack" if "backpack" in normalized else None
        with database() as connection:
            carrying_search = bool(
                label and ("carrying" in normalized or "carry" in normalized)
            )
            if carrying_search:
                rows = connection.execute(
                    """
                    SELECT * FROM observations WHERE label = ?
                    ORDER BY timestamp_seconds LIMIT 100
                    """,
                    (label,),
                ).fetchall()
            elif person_id:
                rows = connection.execute(
                    """
                    SELECT * FROM observations WHERE person_id = ?
                    ORDER BY timestamp_seconds LIMIT 100
                    """,
                    (person_id,),
                ).fetchall()
            elif label:
                rows = connection.execute(
                    """
                    SELECT * FROM observations WHERE label = ?
                    ORDER BY timestamp_seconds LIMIT 100
                    """,
                    (label,),
                ).fetchall()
            elif "people" in normalized or "person" in normalized:
                rows = connection.execute(
                    """
                    SELECT * FROM observations WHERE label = 'person'
                    ORDER BY timestamp_seconds LIMIT 100
                    """
                ).fetchall()
            else:
                rows = []
            people_by_frame = {}
            if carrying_search:
                for backpack in rows:
                    key = (backpack["session_id"], backpack["frame_index"])
                    if key not in people_by_frame:
                        people_by_frame[key] = [
                            person["person_id"]
                            for person in connection.execute(
                                """
                                SELECT DISTINCT person_id FROM observations
                                WHERE session_id = ? AND frame_index = ?
                                  AND label = 'person'
                                """,
                                key,
                            ).fetchall()
                        ]
        results = []
        for row in rows:
            item = {
                "source_id": row["analysis_id"] or row["session_id"],
                "session_id": row["session_id"],
                "source_type": "video" if row["analysis_id"] else "camera",
                "timestamp_seconds": row["timestamp_seconds"],
                "title": row["label"].title(),
                "event_type": "OBJECT_DETECTED",
                "person_id": row["person_id"],
                "description": (
                    f"{row['label'].title()} detected at "
                    f"{row['confidence']:.0%} confidence."
                ),
            }
            if carrying_search:
                nearby_people = [
                    person_id
                    for person_id in people_by_frame.get(
                        (row["session_id"], row["frame_index"]), []
                    )
                    if person_id
                ]
                if person_id and person_id not in nearby_people:
                    continue
                item["person_id"] = None
                item["description"] = (
                    f"Backpack and {len(nearby_people)} person detection(s)"
                    f"{' (' + ', '.join(nearby_people) + ')' if nearby_people else ''} occurred "
                    "in the same sampled frame; this does not confirm who is carrying it."
                )
            results.append(item)
        if person_id and "event" in normalized:
            with database() as connection:
                incident_ids = connection.execute(
                    """
                    SELECT id FROM incidents WHERE person_id = ?
                      AND confidence >= ?
                    ORDER BY timestamp_seconds LIMIT 100
                    """,
                    (person_id, MIN_INCIDENT_CONFIDENCE),
                ).fetchall()
            results.extend(
                public_incident(incident)
                for row in incident_ids
                if (incident := get_incident(row["id"])) is not None
            )
    message = (
        "Search results come from stored detections and incident timestamps."
        if results
        else "No matching stored detections or incidents were found."
    )
    return {
        "query": query,
        "results": results,
        "message": message,
        "needs_visual_analysis": False,
    }


@app.get("/")
def frontend():
    build = ROOT / "dist"
    if (build / "index.html").is_file():
        return FileResponse(build / "index.html")
    return FileResponse(ROOT / "index.html")


@app.get("/login")
def login_page():
    return FileResponse(ROOT / "login.html", media_type="text/html")


@app.get("/styles.css")
def frontend_styles():
    return FileResponse(ROOT / "styles.css", media_type="text/css")


@app.get("/app.js")
def frontend_script():
    return FileResponse(ROOT / "app.js", media_type="text/javascript")


@app.get("/{asset_path:path}")
def frontend_assets(asset_path: str):
    build_asset = (ROOT / "dist" / asset_path).resolve()
    build_root = (ROOT / "dist").resolve()
    if build_root in build_asset.parents and build_asset.is_file():
        return FileResponse(build_asset)
    raise HTTPException(status_code=404, detail="Asset not found.")
