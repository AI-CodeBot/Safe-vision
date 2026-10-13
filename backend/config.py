import math
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def resolve_data_dir():
    candidates = []
    env_value = os.getenv("SAFEVISION_DATA_DIR")
    if env_value:
        candidates.append(Path(env_value).expanduser())
    candidates.extend([ROOT / "data", Path("/tmp/safevision-data")])

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".safevision-write-test"
            probe.touch(exist_ok=True)
            probe.unlink(missing_ok=True)
            return candidate.resolve()
        except OSError:
            continue
    return (ROOT / "data").resolve()


DATA_DIR = resolve_data_dir()
ANALYSES_DIR = DATA_DIR / "analyses"
DATABASE_PATH = DATA_DIR / "safevision.sqlite3"
MODEL_NAME = os.getenv("SAFEVISION_MODEL", "yolo11n.pt")
DETECTION_CONFIDENCE_THRESHOLD = float(
    os.getenv("SAFEVISION_DETECTION_CONFIDENCE", "0.6")
)
if (
    not math.isfinite(DETECTION_CONFIDENCE_THRESHOLD)
    or not 0.1 <= DETECTION_CONFIDENCE_THRESHOLD <= 1.0
):
    raise ValueError("SAFEVISION_DETECTION_CONFIDENCE must be between 0.1 and 1.0.")
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
MAX_SAMPLE_INTERVAL = 5.0
MIN_INCIDENT_CONFIDENCE = 0.9

DATA_DIR.mkdir(parents=True, exist_ok=True)
ANALYSES_DIR.mkdir(parents=True, exist_ok=True)
