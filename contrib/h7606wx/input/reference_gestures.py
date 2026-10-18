# SPDX-License-Identifier: GPL-2.0-only
"""Reference model for review; no production adapter or hardware access.

Derived from the locally tested H7606WX handler. This is a design reference,
not a proposed replacement for the upstream multi-layout runtime.
"""
from dataclasses import dataclass
import math
from libevdev import EV_ABS, EV_KEY, EV_SYN

@dataclass(frozen=True)
class Contact:
    identity: tuple
    x: float
    y: float

class TouchFrames:
    """Commit type-B multitouch state only at SYN_REPORT boundaries."""

    def __init__(self):
        self.slot = 0
        self.slots = {}
        self.positions = {}
        self.has_tracking = False
        self.down = False
        self.x = self.y = None
        self.sequence = 0

    @classmethod
    def from_device(cls, physical):
        frames = cls()
        frames.slot = physical.current_slot or 0
        frames.x = physical.value[EV_ABS.ABS_X]
        frames.y = physical.value[EV_ABS.ABS_Y]
        frames.down = bool(physical.value[EV_KEY.BTN_TOUCH])
        if physical.num_slots is not None:
            frames.has_tracking = True
            for number, slot in enumerate(physical.slots):
                position = {"x": slot[EV_ABS.ABS_MT_POSITION_X], "y": slot[EV_ABS.ABS_MT_POSITION_Y]}
                frames.positions[number] = position
                tracking_id = slot[EV_ABS.ABS_MT_TRACKING_ID]
                if tracking_id is not None and tracking_id >= 0:
                    frames.slots[number] = {"id": tracking_id, **position}
        return frames

    def contacts(self):
        if self.has_tracking:
            return [Contact((slot, state["id"]), state["x"], state["y"])
                    for slot, state in sorted(self.slots.items())
                    if state["x"] is not None and state["y"] is not None]
        if self.down and self.x is not None and self.y is not None:
            return [Contact((0, self.sequence), self.x, self.y)]
        return []

    def feed(self, event):
        code, value = event.code, event.value
        if code == EV_ABS.ABS_MT_SLOT:
            self.slot = value
        elif code == EV_ABS.ABS_MT_TRACKING_ID:
            self.has_tracking = True
            if value < 0:
                self.slots.pop(self.slot, None)
            else:
                self.slots[self.slot] = {"id": value, **self.positions.get(self.slot, {"x": None, "y": None})}
        elif code in (EV_ABS.ABS_MT_POSITION_X, EV_ABS.ABS_MT_POSITION_Y):
            axis = "x" if code == EV_ABS.ABS_MT_POSITION_X else "y"
            self.positions.setdefault(self.slot, {"x": None, "y": None})[axis] = value
            if self.slot in self.slots:
                self.slots[self.slot][axis] = value
        elif code == EV_KEY.BTN_TOUCH:
            if value and not self.down:
                self.sequence += 1
            self.down = bool(value)
        elif code == EV_ABS.ABS_X:
            self.x = value
        elif code == EV_ABS.ABS_Y:
            self.y = value
        if code != EV_SYN.SYN_REPORT:
            return None
        return self.contacts()

class Gestures:
    def __init__(self, geometry, names, emit, hold=1.0, step=20):
        self.cx, self.cy, self.radius, self.center_radius, self.icon_left, self.icon_bottom = geometry
        self.names = names
        self.emit = emit
        self.hold = hold
        self.step = max(5, float(step))
        self.enabled = False
        self.selected = names[0]
        self.control = False
        self.gesture = None
        self.captured = False
        self.blocked = False

    def capture(self, enabled):
        if enabled != self.captured:
            self.captured = enabled
            self.emit("capture", enabled)

    def cancel(self):
        self.gesture = None
        self.capture(False)

    def set_enabled(self, enabled):
        if self.enabled == enabled:
            return
        self.enabled = enabled
        self.control = False
        self.cancel()
        self.emit("display", (self.selected, self.control))

    def angle(self, contact):
        return (math.degrees(math.atan2(contact.y - self.cy, contact.x - self.cx)) + 90) % 360

    def update(self, now, contacts):
        # Resting fingers/palms elsewhere on this large touchpad must not block
        # a dial gesture or its activation icon. Keep the active contact when
        # it leaves the region so that movement can still cancel that gesture.
        active_id = self.gesture["id"] if self.gesture else None
        contacts = [contact for contact in contacts
                    if contact.identity == active_id
                    or (contact.x >= self.icon_left and 0 <= contact.y <= self.icon_bottom)
                    or (self.enabled and math.hypot(contact.x - self.cx, contact.y - self.cy)
                        <= self.radius * 1.15)]
        if len(contacts) != 1:
            if not contacts and self.gesture and self.gesture["kind"] == "center":
                if self.gesture["valid"]:
                    self.control = not self.control
                    self.emit("display", (self.selected, self.control))
            self.cancel()
            self.blocked = bool(contacts)
            return
        if self.blocked:
            return
        contact = contacts[0]
        if self.gesture and self.gesture["id"] != contact.identity:
            # A replaced tracking ID is not a center tap or a continuous turn.
            self.cancel()
        distance = math.hypot(contact.x - self.cx, contact.y - self.cy)
        in_icon = contact.x >= self.icon_left and 0 <= contact.y <= self.icon_bottom
        if self.gesture is None:
            kind = "icon" if in_icon else "ignore"
            if self.enabled and distance <= self.radius * 1.15:
                kind = "center" if distance <= self.center_radius * 1.1 else "ring"
            self.gesture = {"id": contact.identity, "kind": kind, "start": now,
                            "valid": True, "toggled": False, "angle": self.angle(contact), "acc": 0.0}
            if kind in ("center", "ring"):
                self.capture(True)
        gesture = self.gesture
        if gesture["kind"] == "icon":
            if not in_icon:
                gesture["valid"] = False
            if gesture["valid"] and not gesture["toggled"] and now - gesture["start"] >= self.hold:
                gesture["toggled"] = True
                self.enabled = not self.enabled
                self.control = False
                self.emit("toggle", self.enabled)
                self.emit("display", (self.selected, self.control))
            return
        if gesture["kind"] == "center":
            if distance > self.center_radius * 1.2:
                gesture["valid"] = False
            return
        if gesture["kind"] != "ring":
            return
        if not self.center_radius <= distance <= self.radius * 1.15:
            gesture["angle"] = None
            return
        angle = self.angle(contact)
        if not self.control:
            selected = self.names[min(int(angle * len(self.names) / 360), len(self.names) - 1)]
            if selected != self.selected or not gesture.get("displayed"):
                self.selected = selected
                gesture["displayed"] = True
                self.emit("display", (selected, False))
        elif gesture["angle"] is not None:
            delta = (angle - gesture["angle"] + 180) % 360 - 180
            # A discontinuity cannot become a sudden volume/brightness jump.
            if abs(delta) <= 90:
                gesture["acc"] += delta
                steps = int(abs(gesture["acc"]) // self.step)
                if steps:
                    direction = "clockwise" if gesture["acc"] > 0 else "counterclockwise"
                    self.emit("rotate", (self.selected, direction, steps))
                    gesture["acc"] -= math.copysign(steps * self.step, gesture["acc"])
        gesture["angle"] = angle

