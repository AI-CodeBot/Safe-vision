import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.config import DETECTION_CONFIDENCE_THRESHOLD
from backend.detector import detect_frame


class FakeTensor:
    def __init__(self, value):
        self.value = value

    def cpu(self):
        return self

    def tolist(self):
        return self.value

    def item(self):
        return self.value


class DetectorConfidenceTests(unittest.TestCase):
    def test_filters_low_confidence_candidates(self):
        frame = object()
        boxes = [
            SimpleNamespace(
                xyxy=[FakeTensor([0, 0, 20, 20])],
                cls=[FakeTensor(0)],
                conf=[FakeTensor(DETECTION_CONFIDENCE_THRESHOLD - 0.01)],
            ),
            SimpleNamespace(
                xyxy=[FakeTensor([1, 1, 21, 21])],
                cls=[FakeTensor(0)],
                conf=[FakeTensor(DETECTION_CONFIDENCE_THRESHOLD + 0.1)],
            ),
        ]
        prediction = SimpleNamespace(boxes=boxes, names={0: "person"})
        model = MagicMock()
        model.predict.return_value = [prediction]

        with patch("backend.detector.get_model", return_value=model):
            detections = detect_frame(frame)

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0]["label"], "person")
        self.assertGreaterEqual(
            detections[0]["confidence"], DETECTION_CONFIDENCE_THRESHOLD
        )
        model.predict.assert_called_once_with(
            frame, verbose=False, conf=DETECTION_CONFIDENCE_THRESHOLD
        )


if __name__ == "__main__":
    unittest.main()