# SafeVision AI

SafeVision keeps the existing browser webcam preview, snapshot, recording, and
video-upload controls, and adds a React operations dashboard backed by a local
FastAPI service. The dashboard never seeds demo incidents or sample statistics.
Counts, charts, timelines, and search results come from saved detector output.

## Run the app

Use Python 3.10+ and Node.js 20+. In one terminal, install and start the API:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pip install --force-reinstall --no-deps opencv-python-headless
uvicorn backend.main:app --reload
```

Ultralytics depends on the GUI `opencv-python` distribution. The additional
headless-wheel install ensures server environments without `libGL` load OpenCV
correctly; the application itself does not open native OpenCV windows.

In another terminal, install and start the React/Vite UI:

```bash
npm install
npm run dev
```

Open <http://localhost:5173>. Vite proxies `/api` requests to
<http://127.0.0.1:8000>. For a single-origin deployment, build the frontend
with `npm run build`; FastAPI then serves the generated `dist/` app.

## Docker deployment

The project includes a Dockerfile and a docker-compose configuration for a
single-container deployment that builds the frontend and runs the FastAPI app.

```bash
cp .env.example .env
# add any required values, especially GEMINI_API_KEY if needed

docker compose up --build
```

Set `SAFEVISION_ACCESS_PASSWORD` in `.env` before starting the container.
Production mode refuses to start without it. The sign-in page uses an HttpOnly,
SameSite session cookie that expires after 12 hours. Compose binds port 8000 on
all interfaces, so the login is required even when forwarding this port. The
container persists SQLite data and uploads under `./data` on the host machine.
Use a long, unique password before making the Codespaces port public; short
passwords such as `Ahmadali` are easy to guess.

## What it does

- Retains browser camera connection, smooth preview, snapshots, and local
  recording. Live AI samples resized camera frames at about 2 FPS in separate
  API requests, so YOLO inference does not block video playback.
- Runs real Ultralytics YOLO inference on uploaded footage and sampled webcam
  frames. Detection labels, confidence, and bounding boxes are returned from
  the model; person IDs use frame-to-frame bounding-box overlap and are not
  identities.
- Lets the operator draw a normalized rectangular restricted zone on the
  current preview. Restricted-zone checks are inactive unless an operator
  draws a zone. Prolonged-presence (15 seconds by default) and crowd (10 people
  by default) thresholds are configurable.
- Creates deduplicated restricted-zone, prolonged-presence, and crowd incidents
  only when enabled rules are met and a person detection passes the configured
  confidence and consecutive-sample thresholds. Restricted-zone entry requires
  a confirmed outside-to-inside transition; people already present when
  monitoring starts are not reported as new entries. Crowd alerts require
  consecutive qualifying samples to trigger and the same number of
  below-threshold samples to re-arm. Rule settings can be reset to defaults
  without deleting incident history. These checks reduce spurious and repeated
  alerts but cannot guarantee that every alert is correct. It saves the
  triggering snapshot, detection metadata, severity, status, and deterministic
  explanation/action in SQLite.
- Detection settings expose a 90–99% minimum confidence, 3–10 consecutive
  samples, and independent switches for each incident rule. “Reset rules”
  restores these settings and the thresholds to defaults, clears the drawn
  zone, and preserves existing incident history. An incident is emitted only
  by the configured rule checks; Gemini does not decide or trigger actions.
- Calls Gemini asynchronously only for newly created incidents when
  `GEMINI_API_KEY` is set. Without the key, or if Gemini fails, the deterministic
  event explanation remains available. Gemini explanations are advisory and
  cannot change event type or severity. No face recognition is performed.
- Provides real-data incident timelines, review/dismiss actions, analytics
  charts, and searches over recorded objects, people, and incidents. New live
  incidents offer review, dismissal, and an email alert to configured security
  staff. Search does not rerun YOLO. Clothing/color questions such as “red
  shirt” explicitly report that the stored detector metadata cannot answer them.
- Produces an annotated MP4 for each successfully analyzed upload. Uploads are
  limited to 500 MB; supported formats are MP4, MOV, AVI, MKV, and WebM.

## Implementation status

The current frontend is React with Vite and JavaScript, styled with Tailwind
CSS; it is not a Next.js/TypeScript app. The API is FastAPI with SQLite storage.
Video detection runs with Ultralytics YOLO on the backend, not TensorFlow.js in
the browser. Gemini provides optional incident summaries and live-frame
descriptions; the saved-incident assistant uses stored metadata, not OpenAI.
Canvas is used for the browser detection overlay. Supabase variables are
placeholders only, and phone/SMS notifications are not implemented.

## Configuration and storage

Copy `.env.example` to `.env` and set only the values you need. Never commit
credentials.

- `GEMINI_API_KEY` enables post-incident explanations; `GEMINI_MODEL` defaults
  to `gemini-2.5-flash`. To enable Gemini, copy `.env.example` to `.env`, set
  `GEMINI_API_KEY` to a key from Google AI Studio, then restart the backend.
  Keep `.env` private and never paste the key into source code, chat, or logs.
  Gemini is called only for incidents that pass the configured detection
  confidence and temporal-confirmation gates. Gemini explains detections; it
  does not perform object detection, decide whether a rule was triggered, or
  change event severity.
- `RESEND_API_KEY`, `RESEND_FROM_EMAIL`, and `SECURITY_ALERT_EMAILS` enable the
  incident “Alert security” action. Use a sender address verified with Resend;
  separate multiple recipient addresses with commas. Successful sends are
  recorded on the incident, and repeat clicks do not resend. Without these
  settings, the UI reports that email alerting is unavailable.
- `SAFEVISION_MODEL` defaults to `yolo11n.pt`, pretrained on the COCO object
  classes (including people, cars, buses, and many common objects). Ultralytics
  downloads weights on first analysis if they are not already available, so the
  first run may need network access. Custom training requires a labeled,
  representative dataset for the target classes and camera conditions; the
  general-purpose pretrained model is not fine-tuned on this application.
- `SAFEVISION_DATA_DIR` changes where SQLite records, uploaded videos, annotated
  videos, and incident snapshots are stored.
- `SAFEVISION_ACCESS_PASSWORD` is required in production and protects application
  pages and API routes except health checks and the sign-in page. Set it only in
  the ignored `.env` file or your host's secret manager. Do not commit it.
- `SAFEVISION_ENV=production` is set by the Docker image and Compose deployment;
  it enables secure session cookies and requires the login password.
- `VITE_API_URL` is optional; leave it blank for the Vite development proxy.
- `SUPABASE_URL` and `SUPABASE_ANON_KEY` are placeholders for a future external
  persistence adapter. No Supabase project is configured, so SQLite is used.

Data is stored under `data/` by default. Keep authentication enabled and
configure data retention before using the app with sensitive footage. Gemini
API keys shared in chat should be revoked and replaced.

## API

- `GET /health` or `/api/health`
- `POST /api/sessions` and `POST /api/start-session`
- `PUT /api/sessions/{id}/settings`
- `POST /api/sessions/{id}/frame` and `POST /api/analyze-frame`
- `POST /api/sessions/{id}/stop` and `POST /api/stop-session/{id}`
- `POST /api/analyses`, `GET /api/analyses/{id}`, and
  `GET /api/analyses/{id}/video`
- `GET /api/incidents`, `GET /api/incidents/{id}`, incident snapshot, review,
  and resolve endpoints
- `GET /api/analytics`
- `POST /api/search`

## Checks

```bash
npm run build
python -m unittest discover -s tests -v
```
