from backend.config import MIN_INCIDENT_CONFIDENCE

DEFAULT_CONFIRMATION_SAMPLES = 3
STATE_RETENTION_SECONDS = 30
INCIDENT_GRACE_SECONDS = 5
DEFAULT_INCIDENT_RULES = {
    "restricted_zone_entry": True,
    "prolonged_presence": True,
    "crowd_threshold_exceeded": True,
}


def point_in_zone(point, zone):
    if not zone:
        return False
    left = min(zone["x"], zone["x"] + zone["width"])
    right = max(zone["x"], zone["x"] + zone["width"])
    top = min(zone["y"], zone["y"] + zone["height"])
    bottom = max(zone["y"], zone["y"] + zone["height"])
    return left <= point[0] <= right and top <= point[1] <= bottom


class IncidentEngine:
    def __init__(self):
        self.zone_states = {}
        self.zone_signature = None
        self.prolonged_people = set()
        self.prolonged_candidates = {}
        self.prolonged_last_seen = {}
        self.crowd_active = False
        self.crowd_above_samples = 0
        self.crowd_below_samples = 0
        self.crowd_above_started_at = None
        self.active_incidents = {}
        self.ended_incidents = []

    def _start_incident(self, key, event, started_at, last_seen):
        event = dict(event)
        event["timestamp_seconds"] = started_at
        event["started_at_seconds"] = started_at
        event["last_seen_timestamp_seconds"] = last_seen
        event["ended_at_seconds"] = None
        event["duration_seconds"] = max(0.0, last_seen - started_at)
        event["incident_key"] = key
        self.active_incidents[key] = event

    def _end_incident(self, key):
        event = self.active_incidents.pop(key, None)
        if event is None:
            return
        event["ended_at_seconds"] = event["last_seen_timestamp_seconds"]
        event["duration_seconds"] = max(
            0.0,
            event["ended_at_seconds"] - event["started_at_seconds"],
        )
        self.ended_incidents.append(event)

    def active_events(self):
        events = []
        for event in self.active_incidents.values():
            current = dict(event)
            current["duration_seconds"] = max(
                0.0,
                current["last_seen_timestamp_seconds"]
                - current["started_at_seconds"],
            )
            events.append(current)
        return events

    def drain_ended_events(self):
        events, self.ended_incidents = self.ended_incidents, []
        return events

    def finalize(self):
        for key in list(self.active_incidents):
            self._end_incident(key)
        return self.drain_ended_events()

    def update(
        self,
        detections,
        *,
        timestamp,
        width,
        height,
        zone=None,
        prolonged_seconds=15,
        crowd_threshold=10,
        confidence_threshold=MIN_INCIDENT_CONFIDENCE,
        confirmation_samples=DEFAULT_CONFIRMATION_SAMPLES,
        grace_seconds=INCIDENT_GRACE_SECONDS,
        incident_rules=None,
    ):
        incident_rules = {**DEFAULT_INCIDENT_RULES, **(incident_rules or {})}
        zone_signature = (
            tuple(zone[key] for key in ("x", "y", "width", "height"))
            if zone
            else None
        )
        configuration = (
            zone_signature,
            prolonged_seconds,
            crowd_threshold,
            confidence_threshold,
            confirmation_samples,
            grace_seconds,
            tuple(sorted(incident_rules.items())),
        )
        if getattr(self, "configuration", None) != configuration:
            for key in list(self.active_incidents):
                self._end_incident(key)
            self.zone_states.clear()
            self.prolonged_people.clear()
            self.prolonged_candidates.clear()
            self.prolonged_last_seen.clear()
            self.crowd_active = False
            self.crowd_above_samples = 0
            self.crowd_below_samples = 0
            self.crowd_above_started_at = None
            self.configuration = configuration
            self.zone_signature = zone_signature

        people = [item for item in detections if item["label"] == "person"]
        qualified_people = [
            person
            for person in people
            if person["confidence"] >= confidence_threshold
        ]
        events = []
        qualified_person_ids = {
            person["person_id"]
            for person in qualified_people
            if person.get("person_id")
        }
        if zone_signature != self.zone_signature:
            self.zone_states.clear()
            self.zone_signature = zone_signature

        for person in qualified_people:
            person_id = person.get("person_id")
            if not person_id:
                continue
            self.prolonged_last_seen[person_id] = timestamp
            prolonged_key = ("PROLONGED_PRESENCE", person_id)
            if prolonged_key in self.active_incidents:
                self.active_incidents[prolonged_key][
                    "last_seen_timestamp_seconds"
                ] = timestamp
            x1, y1, x2, y2 = person["bbox"]
            center = ((x1 + x2) / (2 * width), (y1 + y2) / (2 * height))
            track = person.get("track", {})
            if zone and incident_rules["restricted_zone_entry"]:
                in_zone = point_in_zone(center, zone)
                zone_state = self.zone_states.setdefault(
                    person_id,
                    {
                        "state": None,
                        "inside_samples": 0,
                        "outside_samples": 0,
                        "last_seen": timestamp,
                        "inside_started_at": None,
                    },
                )
                zone_state["last_seen"] = timestamp
                if in_zone:
                    zone_state["outside_samples"] = 0
                    active_key = ("RESTRICTED_ZONE_ENTRY", person_id)
                    if active_key in self.active_incidents:
                        self.active_incidents[active_key][
                            "last_seen_timestamp_seconds"
                        ] = timestamp
                    if zone_state["state"] == "outside":
                        if zone_state["inside_samples"] == 0:
                            zone_state["inside_started_at"] = timestamp
                        zone_state["inside_samples"] += 1
                        if zone_state["inside_samples"] >= confirmation_samples:
                            event = {
                                    "event_type": "RESTRICTED_ZONE_ENTRY",
                                    "severity": "HIGH",
                                    "confidence": person["confidence"],
                                    "person_id": person_id,
                                    "description": f"{person_id} crossed into the configured restricted zone, confirmed across {confirmation_samples} sampled frames.",
                                    "ai_explanation": f"{person_id} was detected entering the configured restricted zone across {confirmation_samples} consecutive sampled frames.",
                                    "recommended_action": "Review the captured frame and verify whether the entry was authorized.",
                                }
                            events.append(event)
                            self._start_incident(
                                active_key,
                                event,
                                zone_state["inside_started_at"] or timestamp,
                                timestamp,
                            )
                            zone_state["state"] = "inside"
                            zone_state["inside_samples"] = 0
                            zone_state["inside_started_at"] = None
                    elif zone_state["state"] is None:
                        zone_state["state"] = "inside"
                        zone_state["inside_samples"] = 0
                else:
                    zone_state["inside_samples"] = 0
                    zone_state["inside_started_at"] = None
                    zone_state["outside_samples"] += 1
                    if zone_state["outside_samples"] >= confirmation_samples:
                        zone_state["state"] = "outside"
                        self._end_incident(("RESTRICTED_ZONE_ENTRY", person_id))

            duration = track.get("duration_seconds", 0)
            if (
                incident_rules["prolonged_presence"]
                and duration >= prolonged_seconds
                and person_id not in self.prolonged_people
            ):
                candidate = self.prolonged_candidates.setdefault(
                    person_id,
                    {"samples": 0, "started_at": None},
                )
                if candidate["samples"] == 0:
                    candidate["started_at"] = max(
                        0.0,
                        track.get("first_seen", timestamp - duration)
                        + prolonged_seconds,
                    )
                candidate["samples"] += 1
                if candidate["samples"] >= confirmation_samples:
                    event = {
                            "event_type": "PROLONGED_PRESENCE",
                            "severity": "MEDIUM",
                            "confidence": person["confidence"],
                            "person_id": person_id,
                            "description": f"{person_id} has remained visible in the monitored area for approximately {round(duration)} seconds, confirmed across {confirmation_samples} sampled frames.",
                            "ai_explanation": f"{person_id} remained visible in the monitored area for approximately {round(duration)} seconds across {confirmation_samples} consecutive sampled frames.",
                            "recommended_action": "Review the sampled footage and determine whether follow-up is appropriate.",
                        }
                    events.append(event)
                    self._start_incident(
                        ("PROLONGED_PRESENCE", person_id),
                        event,
                        candidate["started_at"],
                        timestamp,
                    )
                    self.prolonged_people.add(person_id)
            else:
                self.prolonged_candidates.pop(person_id, None)

        for person_id in list(self.prolonged_candidates):
            if (
                person_id not in qualified_person_ids
                and timestamp - self.prolonged_last_seen.get(person_id, timestamp)
                > grace_seconds
            ):
                self.prolonged_candidates.pop(person_id, None)
        for person_id, state in self.zone_states.items():
            if (
                person_id not in qualified_person_ids
                and timestamp - state["last_seen"] >= grace_seconds
            ):
                state["inside_samples"] = 0
                state["outside_samples"] = 0

        expired_zone_states = [
            person_id
            for person_id, state in self.zone_states.items()
            if timestamp - state["last_seen"] > STATE_RETENTION_SECONDS
        ]
        for person_id in expired_zone_states:
            del self.zone_states[person_id]

        expired_prolonged_people = [
            person_id
            for person_id, last_seen in self.prolonged_last_seen.items()
            if timestamp - last_seen > grace_seconds
        ]
        for person_id in expired_prolonged_people:
            self._end_incident(("PROLONGED_PRESENCE", person_id))
            del self.prolonged_last_seen[person_id]
            self.prolonged_people.discard(person_id)
            self.prolonged_candidates.pop(person_id, None)

        if incident_rules["crowd_threshold_exceeded"] and len(qualified_people) >= crowd_threshold:
            if self.crowd_above_samples == 0:
                self.crowd_above_started_at = timestamp
            self.crowd_above_samples += 1
            self.crowd_below_samples = 0
        else:
            self.crowd_below_samples += 1
            self.crowd_above_samples = 0
            self.crowd_above_started_at = None

        if (
            self.crowd_above_samples >= confirmation_samples
            and not self.crowd_active
        ):
            confidences = [item["confidence"] for item in qualified_people]
            event = {
                    "event_type": "CROWD_THRESHOLD_EXCEEDED",
                    "severity": "MEDIUM",
                    "confidence": sum(confidences) / len(confidences),
                    "person_id": None,
                    "description": f"{len(qualified_people)} people detected at or above {confidence_threshold:.0%} confidence in each of {confirmation_samples} consecutive sampled frames; configured threshold is {crowd_threshold}.",
                    "ai_explanation": f"{len(qualified_people)} people were detected at or above {confidence_threshold:.0%} confidence across {confirmation_samples} consecutive sampled frames, meeting the configured threshold of {crowd_threshold}.",
                    "recommended_action": "Review the captured frame and verify whether the crowd size requires attention.",
                }
            events.append(event)
            self._start_incident(
                ("CROWD_THRESHOLD_EXCEEDED", None),
                event,
                self.crowd_above_started_at or timestamp,
                timestamp,
            )
            self.crowd_active = True
        elif self.crowd_active and len(qualified_people) >= crowd_threshold:
            self.active_incidents[("CROWD_THRESHOLD_EXCEEDED", None)][
                "last_seen_timestamp_seconds"
            ] = timestamp
        elif (
            self.crowd_active
            and self.crowd_below_samples >= confirmation_samples
            and timestamp
            - self.active_incidents[("CROWD_THRESHOLD_EXCEEDED", None)][
                "last_seen_timestamp_seconds"
            ]
            >= grace_seconds
        ):
            self._end_incident(("CROWD_THRESHOLD_EXCEEDED", None))
            self.crowd_active = False

        for key, event in list(self.active_incidents.items()):
            if key[0] == "RESTRICTED_ZONE_ENTRY" and key[1] not in qualified_person_ids:
                last_seen = event["last_seen_timestamp_seconds"]
                if timestamp - last_seen >= grace_seconds:
                    self._end_incident(key)

        if self.crowd_active and ("CROWD_THRESHOLD_EXCEEDED", None) in self.active_incidents:
            self.active_incidents[("CROWD_THRESHOLD_EXCEEDED", None)][
                "duration_seconds"
            ] = max(
                0.0,
                self.active_incidents[("CROWD_THRESHOLD_EXCEEDED", None)][
                    "last_seen_timestamp_seconds"
                ]
                - self.active_incidents[("CROWD_THRESHOLD_EXCEEDED", None)][
                    "started_at_seconds"
                ],
            )

        return events
