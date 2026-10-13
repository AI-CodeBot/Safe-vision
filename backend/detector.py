import logging
import threading
from importlib.util import find_spec

import cv2

from backend.config import DETECTION_CONFIDENCE_THRESHOLD, MODEL_NAME


logger = logging.getLogger("safevision.detector")
_model = None
_model_lock = threading.Lock()


class DetectorUnavailable(RuntimeError):
    pass


def get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    from ultralytics import YOLO
                except ImportError as error:
                    raise DetectorUnavailable(
                        "Ultralytics is not installed. Install the dependencies in requirements.txt."
                    ) from error
                try:
                    logger.info("Loading YOLO model %s", MODEL_NAME)
                    _model = YOLO(MODEL_NAME)
                except Exception as error:
                    raise DetectorUnavailable(
                        f"Could not load YOLO model {MODEL_NAME}: {error}"
                    ) from error
    return _model


def detector_status():
    try:
        package_available = find_spec("ultralytics") is not None
    except (ImportError, ValueError):
        package_available = False
    return {
        "model": MODEL_NAME if _model is not None else None,
        "model_ready": _model is not None,
        "dependency_available": package_available,
    }


def detect_frame(frame):
    try:
        prediction = get_model().predict(
            frame, verbose=False, conf=DETECTION_CONFIDENCE_THRESHOLD
        )[0]
    except DetectorUnavailable:
        raise
    except Exception as error:
        raise DetectorUnavailable(f"YOLO inference failed: {error}") from error

    detections = []
    for box in prediction.boxes:
        confidence = float(box.conf[0].item())
        if confidence < DETECTION_CONFIDENCE_THRESHOLD:
            continue
        x1, y1, x2, y2 = box.xyxy[0].cpu().tolist()
        class_id = int(box.cls[0].item())
        detections.append(
            {
                "label": str(prediction.names[class_id]),
                "confidence": confidence,
                "bbox": [float(x1), float(y1), float(x2), float(y2)],
            }
        )
    return detections


def annotate_frame(frame, detections):
    for detection in detections:
        x1, y1, x2, y2 = (int(value) for value in detection["bbox"])
        color = (75, 190, 105) if detection["label"] == "person" else (221, 164, 85)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        label = detection["label"]
        if detection.get("person_id"):
            label = f"{detection['person_id']} · {label}"
        label = f"{label} {detection['confidence']:.0%}"
        (text_width, text_height), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1
        )
        top = max(0, y1 - text_height - baseline - 6)
        cv2.rectangle(
            frame,
            (x1, top),
            (x1 + text_width + 8, top + text_height + baseline + 5),
            (32, 51, 46),
            -1,
        )
        cv2.putText(
            frame,
            label,
            (x1 + 4, top + text_height + 1),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
    return frame
