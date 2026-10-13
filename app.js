const video = document.querySelector("#camera-video");
const stage = document.querySelector("#video-stage");
const monitorButton = document.querySelector("#monitor-button");
const monitorLabel = document.querySelector("#monitor-label");
const connectButton = document.querySelector("#connect-button");
const snapshotButton = document.querySelector("#snapshot-button");
const recordButton = document.querySelector("#record-button");
const recordLabel = document.querySelector("#record-label");
const cameraSelect = document.querySelector("#device-select");
const footageInput = document.querySelector("#footage-input");
const eventsList = document.querySelector("#activity-list");
const analyzeButton = document.querySelector("#analyze-button");
const dropZone = document.querySelector("#drop-zone");
const progressPanel = document.querySelector("#analysis-progress");
const resultsPanel = document.querySelector("#analysis-results");
const API_BASE = (import.meta.env?.VITE_API_URL || "").replace(/\/$/, "");

function redirectToLogin(response) {
  if (response.status !== 401) return false;
  window.location.replace("/login.html");
  return true;
}

let stream = null;
let recorder = null;
let recordingChunks = [];
let recordingStartedAt = 0;
let recordingTimer = null;
let toastTimer = null;
let clockTimer = null;
let eventCount = 0;
let reviewUrl = null;
let viewMode = "disconnected";
let selectedVideoFile = null;
let activeAnalysisId = null;

const MAX_VIDEO_SIZE = 500 * 1024 * 1024;

function setClock() {
  const now = new Date();
  document.querySelector("#clock").textContent = now.toLocaleString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
  document.querySelector("#stage-time").textContent = now.toLocaleTimeString([], { hour12: false });
}

function showToast(message) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 2800);
}

function addEvent(title, detail, kind = "normal") {
  const empty = eventsList.querySelector(".empty-activity");
  if (empty) empty.remove();

  const item = document.createElement("li");
  item.className = "activity-event";
  const marker = document.createElement("span");
  marker.className = `event-marker${kind === "warning" ? " warning" : kind === "neutral" ? " neutral" : ""}`;
  const copy = document.createElement("div");
  copy.className = "event-copy";
  const heading = document.createElement("strong");
  heading.textContent = title;
  const description = document.createElement("p");
  description.textContent = detail;
  const time = document.createElement("time");
  time.dateTime = new Date().toISOString();
  time.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  copy.append(heading, description, time);
  item.append(marker, copy);
  eventsList.prepend(item);

  eventCount += 1;
  document.querySelector("#event-count").textContent = String(eventCount);
  document.querySelector("#event-total").textContent = String(eventCount).padStart(2, "0");
  while (eventsList.children.length > 8) eventsList.lastElementChild.remove();
}

function updateDeviceName() {
  if (!stream) return;
  const track = stream.getVideoTracks()[0];
  const settings = track?.getSettings() ?? {};
  const selected = cameraSelect.selectedOptions[0]?.textContent;
  document.querySelector("#device-name").textContent = selected || track?.label || "Connected camera";
  document.querySelector("#device-detail").textContent = settings.width && settings.height
    ? `${settings.width} × ${settings.height} · Permission granted`
    : "Permission granted · Local stream";
}

async function refreshDevices(selectedId) {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  const devices = await navigator.mediaDevices.enumerateDevices();
  const cameras = devices.filter((device) => device.kind === "videoinput");
  cameraSelect.replaceChildren();
  cameras.forEach((camera, index) => {
    const option = document.createElement("option");
    option.value = camera.deviceId;
    option.textContent = camera.label || `Camera ${index + 1}`;
    cameraSelect.append(option);
  });
  cameraSelect.disabled = cameras.length < 2;
  cameraSelect.value = selectedId || cameras[0]?.deviceId || "";
}

function setConnectedState() {
  viewMode = "live";
  stage.classList.add("is-playing");
  stage.classList.remove("is-reviewing");
  video.controls = false;
  document.querySelector("#view-title").textContent = "Live camera";
  document.querySelector("#view-subtitle").textContent = "Streaming directly from this device";
  document.querySelector("#source-label").textContent = "LOCAL CAMERA";
  document.querySelector(".source-tag").classList.remove("reviewing");
  document.querySelector("#quality-label").textContent = "LIVE · LOCAL";
  document.querySelector("#quality-label").classList.add("connected");
  document.querySelector("#live-mark").classList.add("connected");
  document.querySelector("#monitor-label").textContent = "Stop monitoring";
  monitorButton.classList.add("is-active");
  document.querySelector("#session-state").textContent = "Monitoring live";
  document.querySelector("#session-detail").textContent = "Laptop camera is connected";
  document.querySelector("#stream-caption").textContent = "Live feed is active";
  snapshotButton.disabled = false;
  recordButton.disabled = !window.MediaRecorder;
  updateDeviceName();
  window.dispatchEvent(new CustomEvent("safevision:camera-changed", { detail: { connected: true } }));
}

function setDisconnectedState() {
  viewMode = "disconnected";
  stage.classList.remove("is-playing", "is-reviewing");
  video.controls = false;
  video.removeAttribute("src");
  video.srcObject = null;
  document.querySelector("#view-title").textContent = "Live camera";
  document.querySelector("#view-subtitle").textContent = "Waiting for a camera connection";
  document.querySelector("#source-label").textContent = "LOCAL CAMERA";
  document.querySelector(".source-tag").classList.remove("reviewing");
  document.querySelector("#quality-label").textContent = "NO SIGNAL";
  document.querySelector("#quality-label").classList.remove("connected");
  document.querySelector("#live-mark").classList.remove("connected");
  document.querySelector("#monitor-label").textContent = "Start monitoring";
  monitorButton.classList.remove("is-active");
  document.querySelector("#session-state").textContent = "Ready when you are";
  document.querySelector("#session-detail").textContent = "No active camera feed";
  document.querySelector("#stream-caption").textContent = "Camera is disconnected";
  document.querySelector("#device-name").textContent = "No camera connected";
  document.querySelector("#device-detail").textContent = "Your browser will ask for camera permission.";
  snapshotButton.disabled = true;
  recordButton.disabled = true;
  cameraSelect.disabled = true;
  cameraSelect.replaceChildren(new Option("Choose camera", ""));
  window.dispatchEvent(new CustomEvent("safevision:camera-changed", { detail: { connected: false } }));
  window.dispatchEvent(new CustomEvent("safevision:video-changed"));
}

async function startCamera(deviceId = "") {
  if (!navigator.mediaDevices?.getUserMedia) {
    showToast("Camera access needs a supported browser on localhost or HTTPS.");
    addEvent("Camera unavailable", "This browser cannot access a camera.", "warning");
    return;
  }

  try {
    if (recorder?.state === "recording") stopRecording();
    if (stream) stream.getTracks().forEach((track) => track.stop());
    if (reviewUrl) URL.revokeObjectURL(reviewUrl);
    reviewUrl = null;
    const constraints = {
      audio: false,
      video: {
        ...(deviceId ? { deviceId: { exact: deviceId } } : { facingMode: "user" }),
        width: { ideal: 1280 },
        height: { ideal: 720 },
      },
    };
    stream = await navigator.mediaDevices.getUserMedia(constraints);
    video.removeAttribute("src");
    video.srcObject = stream;
    await video.play();
    const activeDeviceId = stream.getVideoTracks()[0]?.getSettings().deviceId;
    await refreshDevices(activeDeviceId || deviceId);
    setConnectedState();
    addEvent("Camera connected", document.querySelector("#device-name").textContent);
    showToast("Camera connected. This feed stays in your browser.");
  } catch (error) {
    stream = null;
    setDisconnectedState();
    const messages = {
      NotAllowedError: "Camera permission was denied. Allow access in your browser settings.",
      NotFoundError: "No camera was found on this device.",
      NotReadableError: "The camera is in use by another application.",
      OverconstrainedError: "That camera is not available. Try another device.",
    };
    const message = messages[error.name] || "Could not connect to the camera. Check browser permissions and try again.";
    showToast(message);
    addEvent("Camera connection failed", message, "warning");
  }
}

function stopCamera() {
  if (recorder?.state === "recording") stopRecording();
  if (stream) stream.getTracks().forEach((track) => track.stop());
  stream = null;
  window.dispatchEvent(new CustomEvent("safevision:camera-changed", { detail: { connected: false } }));
  if (reviewUrl) URL.revokeObjectURL(reviewUrl);
  reviewUrl = null;
  setDisconnectedState();
  addEvent("Monitoring stopped", "Camera session ended.", "neutral");
}

window.safeVisionStopCamera = stopCamera;

function takeSnapshot() {
  if (!stream || !video.videoWidth) return;
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  canvas.toBlob((blob) => {
    if (!blob) {
      showToast("Could not capture this frame. Try again.");
      return;
    }
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `safevision-snapshot-${new Date().toISOString().replaceAll(":", "-")}.jpg`;
    link.click();
    URL.revokeObjectURL(link.href);
    addEvent("Snapshot captured", "Saved to your device as a JPEG.");
    showToast("Snapshot downloaded to your device.");
  }, "image/jpeg", 0.92);
}

function formatDuration(milliseconds) {
  const totalSeconds = Math.floor(milliseconds / 1000);
  const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, "0");
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function downloadRecording() {
  if (!recordingChunks.length) return;
  const mimeType = recordingChunks[0].type || "video/webm";
  const blob = new Blob(recordingChunks, { type: mimeType });
  const extension = mimeType.includes("mp4") ? "mp4" : "webm";
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `safevision-recording-${new Date().toISOString().replaceAll(":", "-")}.${extension}`;
  link.click();
  URL.revokeObjectURL(link.href);
  const duration = formatDuration(Date.now() - recordingStartedAt);
  addEvent("Recording saved", `${duration} clip downloaded to your device.`);
  showToast("Recording downloaded to your device.");
  recordingChunks = [];
}

function startRecording() {
  if (!stream || !window.MediaRecorder) return;
  const supportedType = ["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"]
    .find((type) => MediaRecorder.isTypeSupported(type));
  try {
    recorder = supportedType ? new MediaRecorder(stream, { mimeType: supportedType }) : new MediaRecorder(stream);
    recordingChunks = [];
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data.size) recordingChunks.push(event.data);
    });
    recorder.addEventListener("stop", downloadRecording, { once: true });
    recorder.start(1000);
    recordingStartedAt = Date.now();
    recordButton.classList.add("recording");
    document.querySelector("#recording-tag").classList.add("visible");
    recordLabel.textContent = "Stop recording";
    addEvent("Recording started", "This clip is being recorded locally.");
    document.querySelector("#record-duration").textContent = "00:00";
    recordingTimer = window.setInterval(() => {
      document.querySelector("#record-duration").textContent = formatDuration(Date.now() - recordingStartedAt);
    }, 1000);
  } catch {
    showToast("Recording is not available in this browser.");
  }
}

function stopRecording() {
  if (recorder?.state !== "recording") return;
  recorder.stop();
  window.clearInterval(recordingTimer);
  recordButton.classList.remove("recording");
  document.querySelector("#recording-tag").classList.remove("visible");
  recordLabel.textContent = "Record";
  document.querySelector("#record-duration").textContent = "";
}

async function reviewFootage(file) {
  if (!file) return;
  selectedVideoFile = file;
  analyzeButton.disabled = false;
  document.querySelector("#upload-prompt").textContent = file.name;
  document.querySelector("#upload-detail").textContent = `${(file.size / (1024 * 1024)).toFixed(1)} MB · Ready to analyze`;
  resultsPanel.hidden = true;
  progressPanel.hidden = true;
  activeAnalysisId = null;
  if (stream) stream.getTracks().forEach((track) => track.stop());
  stream = null;
  window.dispatchEvent(new CustomEvent("safevision:camera-changed", { detail: { connected: false } }));
  if (reviewUrl) URL.revokeObjectURL(reviewUrl);
  reviewUrl = URL.createObjectURL(file);
  video.srcObject = null;
  video.src = reviewUrl;
  video.controls = true;
  stage.classList.add("is-playing", "is-reviewing");
  viewMode = "review";
  document.querySelector("#view-title").textContent = "Clip review";
  document.querySelector("#view-subtitle").textContent = file.name;
  document.querySelector("#source-label").textContent = "LOCAL FILE";
  document.querySelector(".source-tag").classList.add("reviewing");
  document.querySelector("#quality-label").textContent = "CLIP REVIEW";
  document.querySelector("#quality-label").classList.remove("connected");
  document.querySelector("#live-mark").classList.remove("connected");
  document.querySelector("#monitor-label").textContent = "Return to live";
  monitorButton.classList.remove("is-active");
  document.querySelector("#session-state").textContent = "Reviewing a clip";
  document.querySelector("#session-detail").textContent = "Playback is local to this device";
  document.querySelector("#stream-caption").textContent = "Local video file · not uploaded";
  document.querySelector("#device-name").textContent = "Local video file";
  document.querySelector("#device-detail").textContent = file.name;
  snapshotButton.disabled = true;
  recordButton.disabled = true;
  cameraSelect.disabled = true;
  await video.play().catch(() => {});
  window.dispatchEvent(new CustomEvent("safevision:video-changed"));
  addEvent("Clip opened", file.name, "neutral");
  showToast("Video preview ready. Analyze it to upload for detection.");
  footageInput.value = "";
}

function updateProgress(progress, phase, detail) {
  const value = Math.max(0, Math.min(100, Number(progress) || 0));
  document.querySelector("#progress-bar").value = value;
  document.querySelector("#progress-percent").textContent = `${Math.round(value)}%`;
  document.querySelector("#progress-label").textContent = phase;
  document.querySelector("#progress-detail").textContent = detail || "";
}

function showAnalysisError(message) {
  progressPanel.hidden = false;
  updateProgress(0, "Analysis failed", message);
  document.querySelector("#progress-label").classList.add("has-error");
  analyzeButton.disabled = !selectedVideoFile;
  showToast(message);
  addEvent("Video analysis failed", message, "warning");
}

async function fetchAnalysis(analysisId) {
  const response = await fetch(`${API_BASE}/api/analyses/${encodeURIComponent(analysisId)}`);
  if (redirectToLogin(response)) throw new Error("Sign in to access SafeVision.");
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "Could not retrieve analysis progress.");
  return data;
}

function renderAnalysisResults(data) {
  resultsPanel.hidden = false;
  const objectCounts = document.querySelector("#object-counts");
  const detectionList = document.querySelector("#detection-list");
  objectCounts.replaceChildren();
  detectionList.replaceChildren();
  const counts = data.object_counts || [];
  if (counts.length) {
    counts.forEach(({ label, count }) => {
      const item = document.createElement("li");
      const name = document.createElement("strong");
      const total = document.createElement("span");
      name.textContent = label;
      total.textContent = `Up to ${count} in one frame`;
      item.append(name, total);
      objectCounts.append(item);
    });
  } else {
    const empty = document.createElement("li");
    empty.className = "results-empty";
    empty.textContent = "No supported objects were detected in the sampled frames.";
    objectCounts.append(empty);
  }

  (data.detections || []).forEach((detection) => {
    const item = document.createElement("li");
    item.textContent = `${detection.label} · highest score ${Math.round(detection.confidence * 100)}% · seen in ${detection.frames_observed} sampled frames`;
    detectionList.append(item);
  });
  document.querySelector("#result-summary").textContent =
    `${data.analyzed_frames} frames analyzed · ${data.detection_count} high-confidence observations · ${data.duration_seconds.toFixed(1)} seconds`;

  const downloadLink = document.querySelector("#download-annotated");
  downloadLink.href = `${API_BASE}${data.video_url}`;
  downloadLink.download = `${selectedVideoFile?.name.replace(/\.[^.]+$/, "") || "video"}-annotated.mp4`;
  downloadLink.hidden = false;

  if (reviewUrl) URL.revokeObjectURL(reviewUrl);
  reviewUrl = null;
  video.srcObject = null;
  video.src = `${API_BASE}${data.video_url}`;
  video.controls = true;
  video.load();
  window.dispatchEvent(new CustomEvent("safevision:video-changed"));
  stage.classList.add("is-playing", "is-reviewing");
  document.querySelector("#view-title").textContent = "Annotated video";
  document.querySelector("#view-subtitle").textContent = "YOLO object detection result";
  document.querySelector("#source-label").textContent = "ANALYZED VIDEO";
  document.querySelector(".source-tag").classList.add("reviewing");
  document.querySelector("#quality-label").textContent = "ANALYSIS COMPLETE";
  document.querySelector("#session-state").textContent = "Video analysis complete";
  document.querySelector("#session-detail").textContent = `${data.detection_count} objects detected`;
  document.querySelector("#stream-caption").textContent = "Annotated video · processed by the backend";
  document.querySelector("#vision-status").textContent = "YOLO analysis complete.";
  addEvent("Video analysis complete", `${data.detection_count} detections across ${data.analyzed_frames} sampled frames.`);
}

async function pollAnalysis(analysisId) {
  while (activeAnalysisId === analysisId) {
    const data = await fetchAnalysis(analysisId);
    if (activeAnalysisId !== analysisId) return;
    updateProgress(10 + data.progress * 0.9, data.phase, data.message);
    if (data.status === "completed") {
      activeAnalysisId = null;
      updateProgress(100, "Analysis complete", "Annotated video and detection results are ready.");
      analyzeButton.disabled = false;
      renderAnalysisResults(data);
      return;
    }
    if (data.status === "failed") throw new Error(data.error || "The video analysis failed.");
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
}

function uploadVideo(file) {
  return new Promise((resolve, reject) => {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("sample_interval", document.querySelector("#sample-interval").value);
    formData.append("settings", JSON.stringify(window.safeVisionSettings || {}));
    const request = new XMLHttpRequest();
    request.open("POST", `${API_BASE}/api/analyses`);
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) {
        const percent = Math.round((event.loaded / event.total) * 10);
        updateProgress(percent, "Uploading video", `${Math.round((event.loaded / (1024 * 1024)) * 10) / 10} MB of ${Math.round((event.total / (1024 * 1024)) * 10) / 10} MB`);
      }
    });
    request.addEventListener("load", () => {
      if (redirectToLogin(request)) {
        reject(new Error("Sign in to access SafeVision."));
        return;
      }
      let data = {};
      try {
        data = JSON.parse(request.responseText);
      } catch {
        reject(new Error("The analysis service returned an invalid response."));
        return;
      }
      if (request.status < 200 || request.status >= 300) {
        reject(new Error(data.detail || "The video could not be uploaded."));
        return;
      }
      resolve(data);
    });
    request.addEventListener("error", () => reject(new Error("Could not reach the analysis service. Start the backend and try again.")));
    request.send(formData);
  });
}

async function analyzeVideo() {
  if (!selectedVideoFile || analyzeButton.disabled) return;
  analyzeButton.disabled = true;
  dropZone.disabled = true;
  document.querySelector("#review-button").disabled = true;
  document.querySelector("#progress-label").classList.remove("has-error");
  document.querySelector("#download-annotated").hidden = true;
  resultsPanel.hidden = true;
  progressPanel.hidden = false;
  updateProgress(0, "Uploading video", "Preparing upload to the local backend.");
  try {
    const job = await uploadVideo(selectedVideoFile);
    activeAnalysisId = job.id;
    dropZone.disabled = false;
    document.querySelector("#review-button").disabled = false;
    document.querySelector("#session-state").textContent = "Analyzing uploaded video";
    document.querySelector("#session-detail").textContent = selectedVideoFile.name;
    document.querySelector("#stream-caption").textContent = "Uploaded to the local analysis service";
    document.querySelector("#vision-status").textContent = "YOLO model is processing video frames.";
    addEvent("Video uploaded", selectedVideoFile.name, "neutral");
    await pollAnalysis(job.id);
  } catch (error) {
    activeAnalysisId = null;
    dropZone.disabled = false;
    document.querySelector("#review-button").disabled = false;
    showAnalysisError(error.message);
  }
}

function acceptVideoFile(file) {
  if (!file) return;
  const supportedExtensions = /\.(mp4|mov|avi|mkv|webm)$/i;
  if (!file.type.startsWith("video/") && !supportedExtensions.test(file.name)) {
    showToast("Choose a supported video file (MP4, MOV, AVI, MKV, or WebM).");
    return;
  }
  if (file.size > MAX_VIDEO_SIZE) {
    showToast("This file exceeds the 500 MB upload limit.");
    return;
  }
  reviewFootage(file);
}

monitorButton.addEventListener("click", () => {
  if (viewMode === "live") stopCamera();
  else startCamera();
});
connectButton.addEventListener("click", () => startCamera());
snapshotButton.addEventListener("click", takeSnapshot);
recordButton.addEventListener("click", () => recorder?.state === "recording" ? stopRecording() : startRecording());
document.querySelector("#review-button").addEventListener("click", () => footageInput.click());
dropZone.addEventListener("click", () => footageInput.click());
footageInput.addEventListener("change", () => acceptVideoFile(footageInput.files[0]));
analyzeButton.addEventListener("click", analyzeVideo);
dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  dropZone.classList.add("is-dragging");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("is-dragging"));
dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropZone.classList.remove("is-dragging");
  acceptVideoFile(event.dataTransfer.files[0]);
});
cameraSelect.addEventListener("change", () => startCamera(cameraSelect.value));
document.querySelector("#clear-events").addEventListener("click", () => {
  eventsList.replaceChildren();
  const empty = document.createElement("li");
  empty.className = "empty-activity";
  empty.innerHTML = "<span class=\"empty-activity-line\"></span><p>Nothing to report yet.</p><small>Camera events will show up here.</small>";
  eventsList.append(empty);
  eventCount = 0;
  document.querySelector("#event-count").textContent = "0";
  document.querySelector("#event-total").textContent = "00";
});
document.querySelector("#fullscreen-button").addEventListener("click", async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await stage.requestFullscreen();
  } catch {
    showToast("Fullscreen is unavailable in this browser.");
  }
});
window.addEventListener("beforeunload", () => {
  stream?.getTracks().forEach((track) => track.stop());
  if (reviewUrl) URL.revokeObjectURL(reviewUrl);
  if (recordingTimer) window.clearInterval(recordingTimer);
});

fetch(`${API_BASE}/api/health`)
  .then((response) => {
    if (!response.ok) throw new Error("Analysis service is unavailable.");
    return response.json();
  })
  .then((health) => {
    document.querySelector("#model-status").textContent = "Local analysis service";
    document.querySelector("#model-label").textContent = health.dependency_available ? "ONLINE" : "NO MODEL";
    document.querySelector("#vision-status").textContent = health.model_ready
      ? `Ready · ${health.model}`
      : health.dependency_available
        ? "Service is online; the YOLO model loads when analysis starts."
        : "Backend is online, but Ultralytics is not installed in its environment.";
  })
  .catch(() => {
    document.querySelector("#model-status").textContent = "Service unavailable";
    document.querySelector("#model-label").textContent = "OFFLINE";
    document.querySelector("#vision-status").textContent = "Start the local backend to enable video analysis.";
  });

fetch(`${API_BASE}/api/analytics`)
  .then((response) => redirectToLogin(response))
  .catch(() => {});

setClock();
clockTimer = window.setInterval(setClock, 1000);
window.addEventListener("beforeunload", () => window.clearInterval(clockTimer));