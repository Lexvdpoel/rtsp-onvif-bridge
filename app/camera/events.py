"""ONVIF event delivery: topics, subscriptions and the PullPoint queue.

An NVR learns that something happened by subscribing to the camera's event
service and pulling messages from it. Until now this device answered every pull
with an empty batch, which is honest for a plain RTSP relay but useless once
there is a detector behind it.

Three things have to line up or nothing arrives:

* the topics have to be ones the NVR recognises. There is no single spelling
  every product agreed on, so each detection is published under several: the
  classic motion property, the cell-motion rule, an ONVIF object detector and
  the per-class rules Hikvision-style clients look for. Publishing one event
  four ways costs nothing and covers far more NVRs than picking a favourite.
* motion has to be a state with a beginning and an end. A client that only ever
  sees "motion started" shows a camera that has been moving since the day it was
  plugged in.
* a subscription has to persist between pulls. PullMessages is a long poll: the
  client asks for up to N messages, waits, and gets whatever accumulated. A
  queue per subscription is what makes that work when two clients watch at once.

What is not here is anything the detector cannot see. There is no tamper
detection, no line crossing, no face or licence plate. Advertising those would
put filters in an NVR that never match.
"""

from __future__ import annotations

import threading
import time
import uuid

from .soap import utc_now

# Detection type -> how each dialect names it.
#
# ObjectType values are the ONVIF ones; the rule names are the spelling
# Hikvision and the clients written against it use, which is what most NVRs
# with a "smart event" checkbox are looking for.
CLASSES = {
    "person": {"object_type": "Human", "rule": "PeopleDetect"},
    "vehicle": {"object_type": "Vehicle", "rule": "VehicleDetect"},
    "animal": {"object_type": "Animal", "rule": "AnimalDetect"},
}

MOTION_TOPIC = "tns1:VideoSource/MotionAlarm"
CELL_MOTION_TOPIC = "tns1:RuleEngine/CellMotionDetector/Motion"
OBJECT_TOPIC = "tns1:RuleEngine/ObjectDetector/Object"
RULE_TOPIC = "tns1:RuleEngine/MyRuleDetector/%s"

# How long a subscription lives without being renewed. ONVIF clients renew well
# inside this; the limit is only there so an NVR that vanishes stops costing
# memory.
DEFAULT_TERMINATION = 600


class Message:
    """One notification, rendered into SOAP when a client pulls it."""

    __slots__ = ("topic", "source", "data", "utc", "property_op")

    def __init__(self, topic: str, source: dict, data: dict,
                 property_op: str = "Changed"):
        self.topic = topic
        self.source = source
        self.data = data
        self.utc = utc_now()
        # Property topics carry an operation; ONVIF clients use it to tell an
        # initial state dump from a real change.
        self.property_op = property_op


class Subscription:
    __slots__ = ("id", "queue", "expires", "lock")

    def __init__(self, sub_id: str, termination: float):
        self.id = sub_id
        self.queue: list[Message] = []
        self.expires = time.monotonic() + termination
        self.lock = threading.Lock()

    def put(self, message: Message, limit: int = 200):
        with self.lock:
            self.queue.append(message)
            if len(self.queue) > limit:
                # A client that stopped pulling must not grow this without end.
                # The newest are the ones worth keeping.
                del self.queue[:-limit]

    def take(self, limit: int) -> list[Message]:
        with self.lock:
            batch, self.queue = self.queue[:limit], self.queue[limit:]
            return batch


class EventBroker:
    """Holds the subscriptions and turns detections into notifications.

    Motion is a state here, not a stream of pings. note_presence is called for
    every analysed frame and keeps the state true while anything is in view;
    when the frames stop reporting anything, a timer ends it. That is separate
    from the detector's own cooldown, which governs how often a *smart* event is
    worth repeating -- without the split, a person standing still would make
    motion flap on and off for as long as they stood there.
    """

    def __init__(self, source_token: str = "VideoSource_1", hold: float = 8.0):
        self.source_token = source_token
        self.hold = hold
        self._subs: dict[str, Subscription] = {}
        self._lock = threading.Lock()
        self._motion_until = 0.0
        self._motion_on = False
        self._active: dict[str, float] = {}
        self._stop = threading.Event()
        self._timer = threading.Thread(target=self._watch, name="events", daemon=True)
        self._timer.start()

    # ------------------------------------------------------------ subscriptions

    def subscribe(self, termination: float = DEFAULT_TERMINATION,
                  who: str = "") -> Subscription:
        sub = Subscription(uuid.uuid4().hex[:12], termination)
        with self._lock:
            self._expire()
            self._subs[sub.id] = sub
            total = len(self._subs)
        # Worth a line each: whether anything is listening is the first question
        # when detections show up here and nowhere else, and a subscription that
        # is made and immediately abandoned looks the same as one never made.
        print(
            f"[events] subscription {sub.id} opened by {who or 'an NVR'}, "
            f"expiring in {int(termination)}s ({total} now)"
        )
        return sub

    def get(self, sub_id: str) -> Subscription | None:
        with self._lock:
            return self._subs.get(sub_id)

    def renew(self, sub_id: str, termination: float = DEFAULT_TERMINATION) -> bool:
        sub = self.get(sub_id)
        if sub is None:
            return False
        sub.expires = time.monotonic() + termination
        return True

    def unsubscribe(self, sub_id: str) -> bool:
        with self._lock:
            gone = self._subs.pop(sub_id, None) is not None
            total = len(self._subs)
        if gone:
            print(f"[events] subscription {sub_id} closed by the NVR ({total} left)")
        return gone

    def _expire(self):
        """Drop subscriptions nothing renewed. Caller holds the lock."""
        now = time.monotonic()
        for sub_id in [k for k, v in self._subs.items() if v.expires < now]:
            del self._subs[sub_id]
            # Not the same as unsubscribing: this is an NVR that stopped
            # renewing without saying so, which is what a crashed or
            # reconfigured one looks like from here.
            print(f"[events] subscription {sub_id} expired; nothing renewed it")

    @property
    def subscribers(self) -> int:
        with self._lock:
            self._expire()
            return len(self._subs)

    # --------------------------------------------------------------- publishing

    def publish(self, message: Message):
        with self._lock:
            self._expire()
            subs = list(self._subs.values())
        for sub in subs:
            sub.put(message)

    def _source(self) -> dict:
        return {"VideoSourceConfigurationToken": self.source_token,
                "VideoAnalyticsConfigurationToken": "VideoAnalytics_1",
                "Rule": "MotionDetector"}

    def _set_motion(self, on: bool):
        if on == self._motion_on:
            return
        self._motion_on = on
        state = "true" if on else "false"
        self.publish(Message(MOTION_TOPIC,
                             {"VideoSourceConfigurationToken": self.source_token},
                             {"State": state}))
        self.publish(Message(CELL_MOTION_TOPIC, self._source(),
                             {"IsMotion": state}))

    def note_presence(self, present: dict):
        """Called for every analysed frame with whatever was above threshold."""
        if present:
            self._motion_until = time.monotonic() + self.hold
            self._set_motion(True)

    def note_detection(self, object_type: str, score: float):
        """A detection worth reporting as its own smart event."""
        info = CLASSES.get(object_type)
        if info is None:
            return
        self._motion_until = time.monotonic() + self.hold
        self._set_motion(True)
        self._active[object_type] = self._motion_until

        self.publish(Message(
            OBJECT_TOPIC,
            {"VideoSourceConfigurationToken": self.source_token,
             "VideoAnalyticsConfigurationToken": "VideoAnalytics_1",
             "Rule": "ObjectDetector"},
            # ObjectId is required by the schema. There is no tracking behind
            # this, so it is not pretended to be stable across events.
            {"ObjectId": str(int(time.time()) % 100000),
             "ObjectType": info["object_type"],
             "Likelihood": f"{score:.3f}"},
        ))
        self.publish(Message(
            RULE_TOPIC % info["rule"],
            {"VideoSourceConfigurationToken": self.source_token,
             "Rule": info["rule"]},
            {"State": "true"},
        ))

    def _end_class(self, object_type: str):
        info = CLASSES.get(object_type)
        if info is None:
            return
        self.publish(Message(
            RULE_TOPIC % info["rule"],
            {"VideoSourceConfigurationToken": self.source_token,
             "Rule": info["rule"]},
            {"State": "false"},
        ))

    def _watch(self):
        """Ends states that nothing has refreshed. Without this they never end."""
        while not self._stop.wait(0.5):
            now = time.monotonic()
            for object_type in [k for k, until in self._active.items() if until < now]:
                del self._active[object_type]
                self._end_class(object_type)
            if self._motion_on and self._motion_until < now:
                self._set_motion(False)

    def stop(self):
        self._stop.set()
