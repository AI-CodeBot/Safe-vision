from dataclasses import dataclass


def intersection_over_union(left, right):
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if intersection == 0:
        return 0.0
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    return intersection / (left_area + right_area - intersection)


@dataclass
class Track:
    person_id: str
    bbox: tuple[float, float, float, float]
    first_seen: float
    last_seen: float

    @property
    def duration_seconds(self):
        return max(0.0, self.last_seen - self.first_seen)

    def as_dict(self):
        return {
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "duration_seconds": self.duration_seconds,
        }


class PersonTracker:
    def __init__(self, match_iou=0.2, max_gap_seconds=3.0):
        self.match_iou = match_iou
        self.max_gap_seconds = max_gap_seconds
        self.tracks = {}
        self.next_person_number = 1

    def update(self, detections, timestamp):
        persons = [
            item for item in detections
            if item["label"] == "person"
        ]
        candidates = []
        for person_id, track in self.tracks.items():
            if timestamp - track.last_seen <= self.max_gap_seconds:
                for index, detection in enumerate(persons):
                    overlap = intersection_over_union(track.bbox, detection["bbox"])
                    if overlap >= self.match_iou:
                        candidates.append((overlap, person_id, index))
        matched_tracks = set()
        matched_detections = set()
        for _, person_id, index in sorted(candidates, reverse=True):
            if person_id in matched_tracks or index in matched_detections:
                continue
            detection = persons[index]
            track = self.tracks[person_id]
            track.bbox = tuple(detection["bbox"])
            track.last_seen = timestamp
            detection["person_id"] = person_id
            detection["track"] = track.as_dict()
            matched_tracks.add(person_id)
            matched_detections.add(index)

        for index, detection in enumerate(persons):
            if index in matched_detections:
                continue
            person_id = f"P-{self.next_person_number:03d}"
            self.next_person_number += 1
            track = Track(
                person_id=person_id,
                bbox=tuple(detection["bbox"]),
                first_seen=timestamp,
                last_seen=timestamp,
            )
            self.tracks[person_id] = track
            detection["person_id"] = person_id
            detection["track"] = track.as_dict()

        expired = [
            person_id
            for person_id, track in self.tracks.items()
            if timestamp - track.last_seen > self.max_gap_seconds
        ]
        for person_id in expired:
            del self.tracks[person_id]

        return detections

    def active_person_ids(self, timestamp):
        return {
            person_id
            for person_id, track in self.tracks.items()
            if timestamp - track.last_seen <= self.max_gap_seconds
        }
