import React, { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  Camera,
  Check,
  CircleHelp,
  Clock3,
  Download,
  Eye,
  FileVideo,
  MessageSquareText,
  Search,
  ShieldAlert,
  Users,
  Video,
  X,
  RotateCcw,
  Send,
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, apiUrl } from "./services/api.js";

const DEFAULT_SETTINGS = {
  restricted_zone: null,
  prolonged_seconds: 15,
  crowd_threshold: 10,
  incident_confidence: 0.9,
  confirmation_samples: 3,
  incident_rules: {
    restricted_zone_entry: true,
    prolonged_presence: true,
    crowd_threshold_exceeded: true,
  },
};
const SEVERITY_COLORS = { HIGH: "#c84d3f", MEDIUM: "#d58a37", LOW: "#839087" };

function formatDuration(value = 0) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

function zoneFromPoints(start, end) {
  const x = Math.min(start.x, end.x);
  const y = Math.min(start.y, end.y);
  return {
    x: Math.min(x, 0.999),
    y: Math.min(y, 0.999),
    width: Math.max(0.001, Math.min(Math.abs(end.x - start.x), 1 - Math.min(x, 0.999))),
    height: Math.max(0.001, Math.min(Math.abs(end.y - start.y), 1 - Math.min(y, 0.999))),
  };
}

function displayEvent(eventType = "") {
  return eventType
    .toLowerCase()
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function useCameraState() {
  const [connected, setConnected] = useState(() => {
    const video = document.querySelector("#camera-video");
    return Boolean(video?.srcObject?.active);
  });

  useEffect(() => {
    const sync = (event) => {
      const video = document.querySelector("#camera-video");
      setConnected(event.detail?.connected ?? Boolean(video?.srcObject?.active));
    };
    window.addEventListener("safevision:camera-changed", sync);
    return () => window.removeEventListener("safevision:camera-changed", sync);
  }, []);

  return connected;
}

function useVideoFrameAvailable(cameraConnected) {
  const [available, setAvailable] = useState(false);
  useEffect(() => {
    const video = document.querySelector("#camera-video");
    if (!video) return undefined;
    const update = () => setAvailable(!video.srcObject && video.readyState >= 2 && video.videoWidth > 0);
    video.addEventListener("loadeddata", update);
    video.addEventListener("emptied", update);
    window.addEventListener("safevision:video-changed", update);
    update();
    return () => {
      video.removeEventListener("loadeddata", update);
      video.removeEventListener("emptied", update);
      window.removeEventListener("safevision:video-changed", update);
    };
  }, [cameraConnected]);
  return cameraConnected || available;
}

function StatCard({ icon: Icon, label, value, detail, tone = "" }) {
  return (
    <article className={`sv-stat ${tone}`}>
      <div className="sv-stat-top"><span>{label}</span><Icon size={17} aria-hidden="true" /></div>
      <strong>{value ?? "—"}</strong>
      <small>{detail}</small>
    </article>
  );
}

function IncidentRow({ incident, selected, onSelect }) {
  return (
    <button
      className={`sv-incident-row${selected ? " selected" : ""}`}
      type="button"
      onClick={() => onSelect(incident)}
    >
      <span className={`sv-severity ${incident.severity?.toLowerCase() || "medium"}`} />
      <span className="sv-incident-copy">
        <strong>{displayEvent(incident.event_type)}</strong>
        <small>{incident.description}</small>
        <time>
          {formatDuration(incident.timestamp_seconds)}
          {incident.ended_at_seconds == null ? " · Ongoing" : ` – ${formatDuration(incident.ended_at_seconds)}`}
          {` · ${Number(incident.duration_seconds || 0).toFixed(1)}s`}
          {` · Detection score ${Math.round((incident.confidence || 0) * 100)}% · ${incident.person_id || displayEvent(incident.source_type)}`}
        </time>
      </span>
      <span className="sv-incident-status">{incident.status}</span>
    </button>
  );
}

function DetectionOverlay({ detections, zone, drawMode, onZoneChange }) {
  const canvasRef = useRef(null);
  const dragRef = useRef(null);
  const [draftZone, setDraftZone] = useState(null);
  const visibleZone = draftZone || zone;

  const getGeometry = useCallback(() => {
    const stage = document.querySelector("#video-stage");
    const video = document.querySelector("#camera-video");
    if (!stage || !video?.videoWidth || !video.videoHeight) return null;
    const bounds = stage.getBoundingClientRect();
    const scale = Math.min(bounds.width / video.videoWidth, bounds.height / video.videoHeight);
    const width = video.videoWidth * scale;
    const height = video.videoHeight * scale;
    return {
      bounds,
      scale,
      width,
      height,
      left: (bounds.width - width) / 2,
      top: (bounds.height - height) / 2,
    };
  }, []);

  useEffect(() => {
    let animationFrame;
    const draw = () => {
      animationFrame = requestAnimationFrame(draw);
      const canvas = canvasRef.current;
      const geometry = getGeometry();
      if (!canvas || !geometry) return;
      const bounds = geometry.bounds;
      const pixelRatio = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(bounds.width * pixelRatio)) {
        canvas.width = Math.round(bounds.width * pixelRatio);
        canvas.height = Math.round(bounds.height * pixelRatio);
      }
      canvas.style.width = `${bounds.width}px`;
      canvas.style.height = `${bounds.height}px`;
      const context = canvas.getContext("2d");
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
      context.clearRect(0, 0, bounds.width, bounds.height);

      if (visibleZone) {
        const x = geometry.left + visibleZone.x * geometry.width;
        const y = geometry.top + visibleZone.y * geometry.height;
        const width = visibleZone.width * geometry.width;
        const height = visibleZone.height * geometry.height;
        context.fillStyle = "rgba(200, 77, 63, 0.12)";
        context.strokeStyle = "#ed806d";
        context.lineWidth = 2;
        context.setLineDash([7, 5]);
        context.fillRect(x, y, width, height);
        context.strokeRect(x, y, width, height);
        context.setLineDash([]);
        context.font = "600 11px sans-serif";
        context.fillStyle = "#fff";
        context.fillText("RESTRICTED ZONE", x + 7, y + 17);
      }

      detections.forEach((detection) => {
        const [x1, y1, x2, y2] = detection.normalized_bbox || [];
        if (![x1, y1, x2, y2].every(Number.isFinite)) return;
        const x = geometry.left + x1 * geometry.width;
        const y = geometry.top + y1 * geometry.height;
        const width = (x2 - x1) * geometry.width;
        const height = (y2 - y1) * geometry.height;
        const color = detection.label === "person" ? "#9ee46c" : "#efb963";
        context.strokeStyle = color;
        context.lineWidth = 2;
        context.strokeRect(x, y, width, height);
        const label = `${detection.person_id ? `${detection.person_id} · ` : ""}${detection.label} ${Math.round(detection.confidence * 100)}%`;
        context.font = "600 12px sans-serif";
        const labelWidth = context.measureText(label).width + 12;
        context.fillStyle = "rgba(23, 34, 29, .88)";
        context.fillRect(x, Math.max(geometry.top, y - 23), labelWidth, 21);
        context.fillStyle = color;
        context.fillText(label, x + 6, Math.max(geometry.top + 15, y - 8));
      });
    };
    draw();
    return () => cancelAnimationFrame(animationFrame);
  }, [detections, getGeometry, visibleZone]);

  const normalizedPoint = (event) => {
    const geometry = getGeometry();
    if (!geometry) return null;
    const localX = event.clientX - geometry.bounds.left - geometry.left;
    const localY = event.clientY - geometry.bounds.top - geometry.top;
    return {
      x: Math.max(0, Math.min(1, localX / geometry.width)),
      y: Math.max(0, Math.min(1, localY / geometry.height)),
    };
  };

  const onPointerDown = (event) => {
    if (!drawMode) return;
    const point = normalizedPoint(event);
    if (!point) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = point;
    setDraftZone(zoneFromPoints(point, point));
  };

  const onPointerMove = (event) => {
    if (!drawMode || !dragRef.current) return;
    const point = normalizedPoint(event);
    if (!point) return;
    setDraftZone(zoneFromPoints(dragRef.current, point));
  };

  const onPointerUp = (event) => {
    if (!drawMode || !dragRef.current) return;
    const start = dragRef.current;
    dragRef.current = null;
    const point = normalizedPoint(event);
    if (!point) return;
    const nextZone = zoneFromPoints(start, point);
    setDraftZone(null);
    onZoneChange(nextZone);
  };

  return (
    <canvas
      ref={canvasRef}
      className={`sv-detection-overlay${drawMode ? " drawing" : ""}`}
      aria-label={drawMode ? "Drag to draw a restricted zone" : "Live object detections"}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={() => { dragRef.current = null; setDraftZone(null); }}
    />
  );
}

export default function App() {
  const cameraConnected = useCameraState();
  const frameAvailable = useVideoFrameAvailable(cameraConnected);
  const [health, setHealth] = useState(null);
  const [analytics, setAnalytics] = useState(null);
  const [incidents, setIncidents] = useState([]);
  const [sessionId, setSessionId] = useState(null);
  const [aiStatus, setAiStatus] = useState("IDLE");
  const [detections, setDetections] = useState([]);
  const [peopleCount, setPeopleCount] = useState(0);
  const [objectsCount, setObjectsCount] = useState(0);
  const [settings, setSettings] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("safevision-settings") || "{}");
      return {
        ...DEFAULT_SETTINGS,
        ...saved,
        incident_rules: {
          ...DEFAULT_SETTINGS.incident_rules,
          ...saved.incident_rules,
        },
      };
    } catch {
      return DEFAULT_SETTINGS;
    }
  });
  const [drawMode, setDrawMode] = useState(false);
  const [searchText, setSearchText] = useState("");
  const [searchResults, setSearchResults] = useState(null);
  const [searchError, setSearchError] = useState("");
  const [analysisLibrary, setAnalysisLibrary] = useState([]);
  const [assistantQuestion, setAssistantQuestion] = useState("What events happened recently?");
  const [assistantAnswer, setAssistantAnswer] = useState(null);
  const [assistantBusy, setAssistantBusy] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const [sampleInterval, setSampleInterval] = useState(1.0);
  const [uploading, setUploading] = useState(false);
  const [selectedIncident, setSelectedIncident] = useState(null);
  const [activeAlert, setActiveAlert] = useState(null);
  const [alertingIncidentId, setAlertingIncidentId] = useState(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const frameStartedAt = useRef(0);
  const settingsRef = useRef(settings);

  const refreshDashboard = useCallback(async () => {
    try {
      const [currentAnalytics, currentIncidents, currentLibrary] = await Promise.all([
        api("/api/analytics"),
        api("/api/incidents?limit=100"),
        api("/api/analyses?limit=10"),
      ]);
      setAnalytics(currentAnalytics);
      setIncidents(currentIncidents.incidents);
      setAnalysisLibrary(currentLibrary.analyses || []);
      setHealth((current) => current || { status: "ok" });
      setMessage((current) => current.startsWith("Backend unavailable:") ? "" : current);
    } catch (error) {
      setMessage(`Backend unavailable: ${error.message}. Camera preview remains available.`);
    }
  }, []);

  useEffect(() => {
    let mounted = true;
    const check = async () => {
      try {
        const result = await api("/api/health");
        if (mounted) setHealth(result);
      } catch {
        if (mounted) setHealth({ status: "offline", model_ready: false });
      }
    };
    check();
    refreshDashboard();
    const timer = window.setInterval(() => {
      check();
      refreshDashboard();
    }, 5000);
    return () => {
      mounted = false;
      window.clearInterval(timer);
    };
  }, [refreshDashboard]);

  useEffect(() => {
    settingsRef.current = settings;
    window.safeVisionSettings = settings;
    localStorage.setItem("safevision-settings", JSON.stringify(settings));
    if (sessionId) {
      api(`/api/sessions/${sessionId}/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(settings),
      }).catch((error) => setMessage(`Could not save detection settings: ${error.message}`));
    }
  }, [settings, sessionId]);

  const stopAnalysis = useCallback(async () => {
    const currentSession = sessionId;
    setSessionId(null);
    setAiStatus("IDLE");
    setDetections([]);
    setPeopleCount(0);
    setObjectsCount(0);
    if (currentSession) {
      try {
        await api(`/api/sessions/${currentSession}/stop`, { method: "POST" });
      } catch (error) {
        setMessage(`Could not stop the AI session cleanly: ${error.message}`);
      }
    }
  }, [sessionId]);

  useEffect(() => {
    if (!cameraConnected && sessionId) stopAnalysis();
  }, [cameraConnected, sessionId, stopAnalysis]);

  useEffect(() => {
    if (!sessionId) return undefined;
    const closeSession = () => navigator.sendBeacon(apiUrl(`/api/sessions/${sessionId}/stop`));
    window.addEventListener("pagehide", closeSession);
    return () => window.removeEventListener("pagehide", closeSession);
  }, [sessionId]);

  const startAnalysis = async () => {
    if (!cameraConnected) {
      setMessage("Connect the camera before starting AI analysis.");
      return;
    }
    setBusy(true);
    setMessage("");
    setAiStatus("PROCESSING");
    try {
      const session = await api("/api/sessions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_name: "Live browser camera",
          settings: settingsRef.current,
        }),
      });
      frameStartedAt.current = performance.now();
      setSessionId(session.id);
      setAiStatus("PROCESSING");
      setMessage("");
    } catch (error) {
      setAiStatus("OFFLINE");
      setMessage(`AI detection is currently unavailable. Camera preview is still active. ${error.message}`);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (!sessionId) return undefined;
    let inFlight = false;
    let stopped = false;
    const video = document.querySelector("#camera-video");
    const processOneFrame = async () => {
      if (stopped || inFlight || !video?.videoWidth || !video?.videoHeight) return;
      inFlight = true;
      try {
        const scale = Math.min(1, 640 / video.videoWidth);
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
        canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
        canvas.getContext("2d", { alpha: false }).drawImage(video, 0, 0, canvas.width, canvas.height);
        const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.78));
        if (!blob || stopped) return;
        const form = new FormData();
        form.append("file", blob, "camera-frame.jpg");
        form.append("timestamp_seconds", String((performance.now() - frameStartedAt.current) / 1000));
        form.append("settings", JSON.stringify(settingsRef.current));
        const result = await api(`/api/sessions/${sessionId}/frame`, { method: "POST", body: form });
        const normalized = result.detections.map((item) => ({
          ...item,
          normalized_bbox: [
            item.bbox[0] / result.width,
            item.bbox[1] / result.height,
            item.bbox[2] / result.width,
            item.bbox[3] / result.height,
          ],
        }));
        setDetections(normalized);
        setPeopleCount(result.people_count);
        setObjectsCount(result.objects_count);
        setAiStatus("ACTIVE");
        if (result.incidents.length) {
          setIncidents((current) => {
            const updated = new Map(current.map((incident) => [incident.id, incident]));
            result.incidents.forEach((incident) => updated.set(incident.id, incident));
            return [...updated.values()].sort((left, right) =>
              Number(right.is_active) - Number(left.is_active)
              || right.timestamp_seconds - left.timestamp_seconds,
            );
          });
          const newIncidents = result.incidents.filter((incident) => incident.is_new);
          if (newIncidents.length) {
            setActiveAlert(newIncidents[0]);
            refreshDashboard();
          }
        }
      } catch (error) {
        setAiStatus("OFFLINE");
        setMessage(`AI processing stopped: ${error.message}. Camera preview is still active.`);
        stopped = true;
        setSessionId(null);
        api(`/api/sessions/${sessionId}/stop`, { method: "POST" }).catch(() => {});
      } finally {
        inFlight = false;
      }
    };
    processOneFrame();
    const timer = window.setInterval(processOneFrame, 500);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [refreshDashboard, sessionId]);

  const updateSettings = (key, value) => {
    setSettings((current) => ({ ...current, [key]: value }));
  };

  const updateIncidentRule = (rule, enabled) => {
    setSettings((current) => ({
      ...current,
      incident_rules: { ...current.incident_rules, [rule]: enabled },
    }));
  };

  const resetDetectionSettings = () => {
    setSettings({
      ...DEFAULT_SETTINGS,
      incident_rules: { ...DEFAULT_SETTINGS.incident_rules },
    });
    setDrawMode(false);
    setMessage("Detection rules reset to defaults. Existing incident history was kept.");
  };

  const updateZone = (zone) => {
    setSettings((current) => ({ ...current, restricted_zone: zone }));
    setDrawMode(false);
  };

  const runSearch = async (event) => {
    event.preventDefault();
    setSearchError("");
    setSearchResults(null);
    setBusy(true);
    try {
      setSearchResults(await api("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: searchText }),
      }));
    } catch (error) {
      setSearchError(error.message);
    } finally {
      setBusy(false);
    }
  };

  const exportAnalyticsCsv = async () => {
    try {
      const response = await fetch(apiUrl("/api/analytics/csv"));
      if (!response.ok) throw new Error("CSV export is unavailable right now.");
      const blob = await response.blob();
      const href = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = href;
      link.download = "safevision-incidents.csv";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(href);
      setMessage("Incident export downloaded.");
    } catch (error) {
      setMessage(error.message);
    }
  };

  const runIncidentAssistant = async (event) => {
    event.preventDefault();
    const liveCamera = event.nativeEvent.submitter?.value === "live";
    setAssistantBusy(true);
    try {
      if (liveCamera) {
        const video = document.querySelector("#camera-video");
        if (!cameraConnected || !video?.srcObject?.active || video.readyState < 2 || !video.videoWidth) {
          throw new Error("Connect the live camera and wait for its picture before asking.");
        }
        const scale = Math.min(1, 1024 / video.videoWidth);
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
        canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
        canvas.getContext("2d", { alpha: false }).drawImage(video, 0, 0, canvas.width, canvas.height);
        const image = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.82));
        if (!image) throw new Error("Could not capture the current camera frame.");
        const form = new FormData();
        form.append("question", assistantQuestion);
        form.append("file", image, "live-camera.jpg");
        const response = await api("/api/live/assistant", { method: "POST", body: form });
        setAssistantAnswer(response);
        setMessage("Gemini answered from the current camera frame.");
        return;
      }
      const response = await api("/api/incidents/assistant", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: assistantQuestion }),
      });
      setAssistantAnswer(response);
      setMessage(response.answer);
    } catch (error) {
      setAssistantAnswer({ answer: `Assistant unavailable: ${error.message}` });
      setMessage(error.message);
    } finally {
      setAssistantBusy(false);
    }
  };

  const handleVideoUpload = async (event) => {
    event.preventDefault();
    const file = event.target.elements.videoFile?.files?.[0];
    if (!file) {
      setUploadError("Choose an MP4, MOV, AVI, MKV, or WebM file.");
      return;
    }
    setUploading(true);
    setUploadError("");
    setMessage("");
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("sample_interval", String(sampleInterval));
      form.append("settings", JSON.stringify(settingsRef.current));
      const result = await api("/api/analyses", { method: "POST", body: form });
      setMessage(`Video queued for analysis. ID: ${result.id}`);
      await refreshDashboard();
    } catch (error) {
      setUploadError(error.message);
      setMessage(error.message);
    } finally {
      setUploading(false);
      event.target.reset();
    }
  };

  const openSourceAt = async (item) => {
    if (item.source_type === "video" && item.source_id) {
      try {
        const analysis = await api(`/api/analyses/${item.source_id}`);
        if (analysis.status !== "completed") {
          setMessage("This video is still processing. Its timestamped footage will be available when analysis completes.");
          return;
        }
      } catch (error) {
        setMessage(`Could not open the analyzed footage: ${error.message}`);
        return;
      }
    }
    setSelectedIncident(item.id ? item : null);
    if (!item.id) {
      setMessage(`${displayEvent(item.event_type)} at ${formatDuration(item.timestamp_seconds)}. This result is based on stored detections.`);
    }
    if (!item.source_id || item.source_type === "camera" || item.source_type === "session") return;
    if (sessionId) stopAnalysis();
    window.safeVisionStopCamera?.();
    const video = document.querySelector("#camera-video");
    const stage = document.querySelector("#video-stage");
    const videoUrl = apiUrl(`/api/analyses/${item.source_id}/video`);
    if (video && !video.src.includes(`/api/analyses/${item.source_id}/video`)) {
      video.srcObject = null;
      video.src = videoUrl;
      video.controls = true;
      video.load();
      video.addEventListener("loadedmetadata", () => {
        video.currentTime = item.timestamp_seconds || 0;
        video.play().catch(() => {});
      }, { once: true });
      stage?.classList.add("is-playing", "is-reviewing");
      document.querySelector("#view-title").textContent = "Incident footage";
      document.querySelector("#view-subtitle").textContent = displayEvent(item.event_type);
      window.dispatchEvent(new CustomEvent("safevision:video-changed"));
    } else if (video?.readyState >= 1) {
      video.currentTime = item.timestamp_seconds || 0;
      video.play().catch(() => {});
    }
    document.querySelector("#live")?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  const updateIncidentStatus = async (incident, status) => {
    try {
      const updated = await api(`/api/incidents/${incident.id}/${status === "REVIEWED" ? "review" : "resolve"}`, {
        method: "POST",
      });
      setIncidents((current) => current.map((item) => item.id === updated.id ? updated : item));
      setSelectedIncident(updated);
      if (status === "RESOLVED") {
        setActiveAlert((current) => current?.id === updated.id ? null : current);
      }
      await refreshDashboard();
      return updated;
    } catch (error) {
      setMessage(`Could not update incident: ${error.message}`);
      return null;
    }
  };

  const alertSecurityGuards = async (incident) => {
    setAlertingIncidentId(incident.id);
    try {
      const updated = await api(`/api/incidents/${incident.id}/alert-security`, { method: "POST" });
      setIncidents((current) => current.map((item) => item.id === updated.id ? updated : item));
      setSelectedIncident((current) => current?.id === updated.id ? updated : current);
      setActiveAlert((current) => current?.id === updated.id ? updated : current);
      setMessage("Security guards were alerted by email.");
      await refreshDashboard();
    } catch (error) {
      setMessage(`Could not alert security guards: ${error.message}`);
    } finally {
      setAlertingIncidentId(null);
    }
  };

  const totals = analytics?.totals || {};
  const apiAvailable = health?.status === "ok";
  const modelAvailable = Boolean(health?.dependency_available);
  const settingsZone = settings.restricted_zone;
  const incidentConfidence = Math.round(settings.incident_confidence * 100);
  const confirmationSamples = settings.confirmation_samples;
  const displayedAiStatus = aiStatus === "IDLE"
    ? !health
      ? "CHECKING"
      : health.status !== "ok"
        ? "OFFLINE"
        : health.model_ready
          ? "READY"
          : health.dependency_available
            ? "AVAILABLE"
            : "UNAVAILABLE"
    : aiStatus;

  return (
    <div className="sv-operations">
      <header className="sv-section-heading" id="analytics">
        <div>
          <p className="eyebrow">LIVE INTELLIGENCE · REAL STORED DATA</p>
          <h2>Security operations</h2>
          <p>Incidents require {incidentConfidence}% confidence in {confirmationSamples} consecutive samples.</p>
        </div>
        <span className={`sv-system-state ${apiAvailable ? "online" : "offline"}`}>
          <span />{apiAvailable ? "API CONNECTED" : "API OFFLINE"}
        </span>
      </header>

      {message && (
        <div className="sv-message" role="status">
          <CircleHelp size={17} /><span>{message}</span>
          <button type="button" aria-label="Dismiss message" onClick={() => setMessage("")}><X size={15} /></button>
        </div>
      )}

      <div className="sv-stats-grid grid grid-cols-2 gap-3 md:grid-cols-3">
        <StatCard icon={Users} label="People detected" value={totals.people_detected} detail="Unique tracked people" />
        <StatCard icon={ShieldAlert} label="Incidents" value={totals.incidents} detail={`${incidentConfidence}%+ · ${confirmationSamples} samples`} />
        <StatCard icon={AlertTriangle} label="High priority" value={totals.high_priority_alerts} detail={`${incidentConfidence}%+ · unresolved`} tone="critical" />
        <StatCard icon={Video} label="Videos analyzed" value={totals.videos_analyzed} detail="Completed video jobs" />
        <StatCard icon={Camera} label="Active camera" value={totals.active_cameras} detail="AI sessions running" />
        <StatCard icon={Eye} label="Detection observations" value={totals.objects_detected} detail="High-confidence boxes across sampled frames" />
      </div>

      <div className="sv-operations-grid">
        <section className="sv-panel sv-live-panel" aria-labelledby="live-ai-heading">
          <div className="sv-panel-heading">
            <div><p className="eyebrow">CAMERA + DETECTION</p><h3 id="live-ai-heading">Live AI analysis</h3></div>
            <div className="sv-live-badges">
              <span className={`sv-camera-badge ${cameraConnected ? "connected" : ""}`}><i />Camera: {cameraConnected ? "CONNECTED" : "DISCONNECTED"}</span>
              <span className={`sv-ai-badge ${displayedAiStatus.toLowerCase()}`}><i />AI: {displayedAiStatus}</span>
            </div>
          </div>
          <div className="sv-live-summary">
            <div><span>People detected</span><strong>{peopleCount}</strong></div>
            <div><span>Objects detected</span><strong>{objectsCount}</strong></div>
          </div>
          <div className="sv-live-actions">
            {sessionId ? (
              <button className="button button-primary" type="button" onClick={stopAnalysis}><X size={15} />Stop AI analysis</button>
            ) : (
              <button className="button button-primary" type="button" disabled={!cameraConnected || busy} onClick={startAnalysis}>
                <Activity size={15} />{busy ? "Connecting…" : "Start AI analysis"}
              </button>
            )}
            <button className={`button button-secondary${drawMode ? " selected" : ""}`} type="button" disabled={!frameAvailable} onClick={() => setDrawMode((active) => !active)}>
              <ShieldAlert size={15} />{drawMode ? "Cancel zone drawing" : "Draw restricted zone"}
            </button>
            {settingsZone && <button className="sv-link-button" type="button" onClick={() => updateSettings("restricted_zone", null)}>Clear zone</button>}
          </div>
          {drawMode && <p className="sv-hint">Drag on the live camera frame to draw the restricted zone.</p>}
          {!cameraConnected && <p className="sv-hint">Connect the existing camera preview to enable live AI analysis. You can still define a zone on a selected video preview. AI runs separately at about 2 frames per second.</p>}
          <div className="sv-model-status">
            <span className={modelAvailable ? "ready" : ""} />
            {health?.model_ready ? `YOLO ACTIVE · ${health.model}` : modelAvailable ? "YOLO ready · model loads on first analysis" : "YOLO unavailable · install backend dependencies"}
          </div>
        </section>

        <section className="sv-panel sv-settings-panel" aria-labelledby="detection-settings-heading">
          <div className="sv-panel-heading">
            <div><p className="eyebrow">EVENT RULES</p><h3 id="detection-settings-heading">Detection settings</h3></div>
            <button className="sv-link-button sv-reset-button" type="button" onClick={resetDetectionSettings}>
              <RotateCcw size={13} /> Reset rules
            </button>
          </div>
          <label className="sv-setting">
            <span><strong>Minimum confidence</strong><small>Higher is stricter (90–99%)</small></span>
            <input
              type="number"
              min="90"
              max="99"
              step="1"
              value={incidentConfidence}
              onChange={(event) => updateSettings(
                "incident_confidence",
                Math.max(90, Math.min(99, Number(event.target.value) || 90)) / 100,
              )}
            />
          </label>
          <label className="sv-setting">
            <span><strong>Confirmation samples</strong><small>Consecutive qualifying frames (3–10)</small></span>
            <input
              type="number"
              min="3"
              max="10"
              step="1"
              value={confirmationSamples}
              onChange={(event) => updateSettings(
                "confirmation_samples",
                Math.max(3, Math.min(10, Math.round(Number(event.target.value) || 3))),
              )}
            />
          </label>
          {[
            ["restricted_zone_entry", "Restricted-zone entry", "Only active when a zone is drawn"],
            ["crowd_threshold_exceeded", "Crowd threshold", "Requires the configured people count"],
            ["prolonged_presence", "Prolonged presence", "Requires the configured duration"],
          ].map(([rule, title, description]) => (
            <label className="sv-rule-toggle" key={rule}>
              <span><strong>{title}</strong><small>{description}</small></span>
              <input
                type="checkbox"
                checked={settings.incident_rules[rule]}
                onChange={(event) => updateIncidentRule(rule, event.target.checked)}
              />
            </label>
          ))}
          <label className="sv-setting">
            <span><strong>Prolonged presence</strong><small>Alert threshold in seconds</small></span>
            <input type="number" min="1" max="3600" value={settings.prolonged_seconds} onChange={(event) => updateSettings("prolonged_seconds", Math.max(1, Number(event.target.value) || 1))} />
          </label>
          <label className="sv-setting">
            <span><strong>Crowd threshold</strong><small>Detected people in one sampled frame</small></span>
            <input type="number" min="2" max="500" value={settings.crowd_threshold} onChange={(event) => updateSettings("crowd_threshold", Math.min(500, Math.max(2, Number(event.target.value) || 2)))} />
          </label>
          <div className="sv-setting-note">
            Object boxes are detections, not incidents. An incident is created only after its enabled rule passes the configured detector-confidence and {confirmationSamples}-sample checks; Gemini cannot trigger incidents or actions. Restricted-zone checks are inactive until you draw a zone, and alerts require a tracked crossing into that area. Tracking uses bounding-box overlap and does not identify people.
            {health && <span> {health.gemini_enabled ? "Gemini enrichment is enabled for qualifying incidents." : "Gemini is not configured; deterministic explanations are used."}</span>}
          </div>
        </section>
      </div>

      <div className="sv-charts-grid">
        <section className="sv-panel" aria-labelledby="event-chart-heading">
          <div className="sv-panel-heading">
            <div><p className="eyebrow">PERSISTED INCIDENTS</p><h3 id="event-chart-heading">Event statistics</h3></div>
            <button className="sv-link-button" type="button" onClick={exportAnalyticsCsv}><Download size={14} /> Export CSV</button>
          </div>
          {analytics?.event_stats?.length ? (
            <div className="sv-chart"><ResponsiveContainer width="100%" height="100%">
              <BarChart data={analytics.event_stats} margin={{ top: 8, right: 8, bottom: 25, left: -20 }}>
                <CartesianGrid stroke="#edf0eb" vertical={false} />
                <XAxis dataKey="name" tickFormatter={displayEvent} angle={-18} textAnchor="end" interval={0} tick={{ fontSize: 9 }} />
                <YAxis allowDecimals={false} tick={{ fontSize: 9 }} />
                <Tooltip formatter={(value) => [value, "Incidents"]} labelFormatter={displayEvent} />
                <Bar dataKey="value" fill="#668658" radius={[3, 3, 0, 0]} />
              </BarChart>
            </ResponsiveContainer></div>
          ) : <p className="sv-empty-chart">No incidents have been recorded yet.</p>}
        </section>
        <section className="sv-panel" aria-labelledby="severity-chart-heading">
          <div className="sv-panel-heading"><div><p className="eyebrow">SEVERITY BREAKDOWN</p><h3 id="severity-chart-heading">Severity distribution</h3></div></div>
          {analytics?.severity?.length ? (
            <div className="sv-chart sv-pie-chart"><ResponsiveContainer width="100%" height="100%">
              <PieChart><Pie data={analytics.severity} dataKey="value" nameKey="name" innerRadius={48} outerRadius={76} paddingAngle={3}>
                {analytics.severity.map((item) => <Cell key={item.name} fill={SEVERITY_COLORS[item.name] || "#839087"} />)}
              </Pie><Tooltip formatter={(value, name) => [value, name]} /></PieChart>
            </ResponsiveContainer><div className="sv-chart-legend">{analytics.severity.map((item) => <span key={item.name}><i style={{ background: SEVERITY_COLORS[item.name] || "#839087" }} />{item.name} · {item.value}</span>)}</div></div>
          ) : <p className="sv-empty-chart">Severity chart will populate from real incidents.</p>}
        </section>
      </div>

      <div className="sv-operations-grid sv-lower-grid">
        <section className="sv-panel" aria-labelledby="upload-heading">
          <div className="sv-panel-heading">
            <div><p className="eyebrow">VIDEO ANALYSIS</p><h3 id="upload-heading">Upload footage</h3></div>
            <FileVideo size={17} />
          </div>
          <form className="sv-upload-form" onSubmit={handleVideoUpload}>
            <label className="sv-file-picker">
              <input type="file" name="videoFile" accept=".mp4,.mov,.avi,.mkv,.webm" />
            </label>
            <label className="sv-setting compact">
              <span><strong>Sample interval</strong><small>Seconds between sampled frames</small></span>
              <input type="number" min="0.1" max="5" step="0.1" value={sampleInterval} onChange={(event) => setSampleInterval(Math.min(5, Math.max(0.1, Number(event.target.value) || 0.1)))} />
            </label>
            <button className="button button-primary" type="submit" disabled={uploading}>
              {uploading ? "Uploading…" : "Analyze video"}
            </button>
          </form>
          {uploadError && <p className="sv-error-copy">{uploadError}</p>}
        </section>

        <section className="sv-panel" aria-labelledby="analysis-library-heading">
          <div className="sv-panel-heading">
            <div><p className="eyebrow">SAVED ANALYSES</p><h3 id="analysis-library-heading">Video library</h3></div>
            <button className="sv-link-button" type="button" onClick={refreshDashboard}>Refresh</button>
          </div>
          <div className="sv-library-list">
            {analysisLibrary.length ? analysisLibrary.map((item) => (
              <button key={item.id} type="button" className="sv-library-item" onClick={() => openSourceAt({ source_type: "video", source_id: item.id, timestamp_seconds: 0, event_type: "VIDEO_ANALYSIS" })}>
                <span><strong>{item.filename}</strong><small>{item.status} · {new Date(item.created_at).toLocaleString()}</small></span>
                <span className="sv-library-meta">{item.duration_seconds ? `${Number(item.duration_seconds).toFixed(1)}s` : item.phase}</span>
              </button>
            )) : <p className="sv-empty-chart">No saved analyses yet. Upload a video to create one.</p>}
          </div>
        </section>

        <section className="sv-panel" aria-labelledby="assistant-heading">
          <div className="sv-panel-heading">
            <div><p className="eyebrow">CONTEXTUAL AI</p><h3 id="assistant-heading">Incident assistant</h3></div>
            <MessageSquareText size={17} />
          </div>
          <form className="sv-assistant-form" onSubmit={runIncidentAssistant}>
            <textarea value={assistantQuestion} onChange={(event) => setAssistantQuestion(event.target.value)} rows={3} placeholder="Ask about saved incidents or what is visible in the live camera." />
            <button type="submit" name="mode" value="incidents" className="button button-secondary" disabled={assistantBusy}>Ask about incidents</button>
            <button type="submit" name="mode" value="live" className="button button-secondary" disabled={assistantBusy || !cameraConnected}>Explain live camera</button>
          </form>
          {assistantAnswer && (
            <div className="sv-assistant-result">
              {assistantAnswer.source === "live_camera" && <small>Gemini · current camera frame</small>}
              <p>{assistantAnswer.answer}</p>
              {assistantAnswer.counts && Object.keys(assistantAnswer.counts).length > 0 && (
                <ul>
                  {Object.entries(assistantAnswer.counts).map(([name, value]) => <li key={name}>{name}: {value}</li>)}
                </ul>
              )}
            </div>
          )}
        </section>

        <section className="sv-panel" id="incident-timeline" aria-labelledby="timeline-heading">
          <div className="sv-panel-heading">
            <div><p className="eyebrow">CONFIDENCE-GATED RULE TRIGGERS</p><h3 id="timeline-heading">Incident timeline</h3></div>
            <button className="sv-icon-button" type="button" aria-label="Refresh incidents" onClick={refreshDashboard}><Activity size={15} /></button>
          </div>
          <div className="sv-timeline-list">
            {incidents.length ? incidents.map((incident) => (
              <IncidentRow key={incident.id} incident={incident} selected={selectedIncident?.id === incident.id} onSelect={openSourceAt} />
            )) : <p className="sv-empty-chart">Incidents appear here only after a configured rule is triggered by actual detections.</p>}
          </div>
        </section>

        <section className="sv-panel sv-search-panel" id="video-search" aria-labelledby="search-heading">
          <div className="sv-panel-heading"><div><p className="eyebrow">STORED METADATA · TIMESTAMPED</p><h3 id="search-heading">Ask your footage</h3></div><Search size={17} /></div>
          <form className="sv-search-form" onSubmit={runSearch}>
            <input value={searchText} onChange={(event) => setSearchText(event.target.value)} placeholder="Find people, zone entries, backpacks…" aria-label="Search analyzed footage" />
            <button type="submit" disabled={!searchText.trim() || busy} aria-label="Search footage"><Search size={16} /></button>
          </form>
          <div className="sv-search-examples">
            {["Find all people", "Find restricted zone entries", "Find backpacks", "Find Person #2", "When was the crowd highest?"].map((example) => (
              <button type="button" key={example} onClick={() => setSearchText(example)}>{example}</button>
            ))}
          </div>
          {searchError && <p className="sv-error-copy">{searchError}</p>}
          {searchResults && (
            <div className="sv-search-results">
              <p>{searchResults.message}</p>
              {searchResults.results.map((result, index) => (
                <button key={`${result.source_id}-${result.timestamp_seconds}-${index}`} type="button" onClick={() => openSourceAt(result)}>
                  <Clock3 size={14} /><strong>{formatDuration(result.timestamp_seconds)}</strong>
                  <span>{displayEvent(result.event_type)}{result.person_id ? ` · ${result.person_id}` : ""}<small>{result.description}</small></span>
                  <ArrowUpRight size={14} />
                </button>
              ))}
            </div>
          )}
          {selectedIncident && (
            <div className="sv-incident-detail" aria-live="polite">
              <div className="sv-panel-heading">
                <div><p className="eyebrow">INCIDENT DETAILS · {selectedIncident.status}</p><h4>{displayEvent(selectedIncident.event_type)}</h4></div>
                <button className="sv-icon-button" type="button" aria-label="Close incident details" onClick={() => setSelectedIncident(null)}><X size={15} /></button>
              </div>
              {selectedIncident.snapshot_url && <img src={apiUrl(selectedIncident.snapshot_url)} alt={`Actual detection snapshot: ${selectedIncident.description}`} />}
              <p>
                Started {formatDuration(selectedIncident.timestamp_seconds)}
                {selectedIncident.ended_at_seconds == null
                  ? " · Ongoing"
                  : ` · Ended ${formatDuration(selectedIncident.ended_at_seconds)}`}
                {` · Duration ${Number(selectedIncident.duration_seconds || 0).toFixed(1)}s`}
              </p>
              <p>{selectedIncident.description}</p>
              <p className="sv-explanation">{selectedIncident.ai_explanation}</p>
              <small>Recommended action: {selectedIncident.recommended_action}</small>
              <div className="sv-detail-actions">
                {!selectedIncident.security_alerted_at && <button type="button" disabled={alertingIncidentId === selectedIncident.id} onClick={() => alertSecurityGuards(selectedIncident)}><Send size={14} />{alertingIncidentId === selectedIncident.id ? "Sending…" : "Alert security"}</button>}
                {selectedIncident.security_alerted_at && <button type="button" disabled><Check size={14} />Security alerted</button>}
                {selectedIncident.status === "NEW" && <button type="button" onClick={() => updateIncidentStatus(selectedIncident, "REVIEWED")}><Eye size={14} />Mark reviewed</button>}
                {selectedIncident.status !== "RESOLVED" && <button type="button" onClick={() => updateIncidentStatus(selectedIncident, "RESOLVED")}><X size={14} />Dismiss</button>}
                {selectedIncident.source_type === "video" && <button type="button" onClick={() => openSourceAt(selectedIncident)}><Video size={14} />Jump to footage</button>}
              </div>
            </div>
          )}
        </section>
      </div>

      {activeAlert && (
        <div className={`sv-alert-toast ${activeAlert.severity?.toLowerCase()}`} role="alert">
          <AlertTriangle size={20} />
          <div>
            <strong>{activeAlert.severity} PRIORITY · {displayEvent(activeAlert.event_type)}</strong>
            <p>{activeAlert.description}</p>
            <div className="sv-toast-actions">
              <button type="button" onClick={() => openSourceAt(activeAlert)}><ArrowUpRight size={13} />Review</button>
              <button type="button" disabled={Boolean(activeAlert.security_alerted_at) || alertingIncidentId === activeAlert.id} onClick={() => alertSecurityGuards(activeAlert)}><Send size={13} />{activeAlert.security_alerted_at ? "Guards alerted" : alertingIncidentId === activeAlert.id ? "Sending…" : "Alert security"}</button>
              {activeAlert.status !== "RESOLVED" && <button type="button" onClick={() => updateIncidentStatus(activeAlert, "RESOLVED")}><X size={13} />Dismiss</button>}
            </div>
          </div>
        </div>
      )}

      {frameAvailable && createPortal(
        <DetectionOverlay detections={detections} zone={settings.restricted_zone} drawMode={drawMode} onZoneChange={updateZone} />,
        document.querySelector("#video-stage"),
      )}
    </div>
  );
}
