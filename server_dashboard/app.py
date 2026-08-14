from __future__ import annotations

import json
import math
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent

WINDOW_SECONDS = 10
OFFLINE_AFTER_SECONDS = 8
QUIET_MAX_DB = 70.0
LOUD_MIN_DB = 75.0

# Exhibition table size. Adjust these to match the real display table.
TABLE_WIDTH_CM = 180
TABLE_DEPTH_CM = 80

# Sensor positions use physical table coordinates.
# x = distance from left, y = distance from front.
# 3-device layout: S1/S2 at the front corners, S3 centered at the back.
SENSORS = {
    "S1": {"name": "S1", "label": "Window-left", "x_cm": 25.0, "y_cm": 60.0},
    "S2": {"name": "S2", "label": "Window-right", "x_cm": 155.0, "y_cm": 60.0},
    "S3": {"name": "S3", "label": "Wall-center", "x_cm": 90.0, "y_cm": 20.0},
}

# Automatic assignment by first contact order.
# First unique device_id -> S1, second -> S2, third -> S3.
DEVICE_ASSIGNMENTS: dict[str, str] = {}
SENSOR_ORDER = ["S1", "S2", "S3"]
assignment_lock = Lock()


def assign_sensor(device_id: str) -> str | None:
    """Return the stable S1-S3 assignment for this device_id."""
    with assignment_lock:
        if device_id in DEVICE_ASSIGNMENTS:
            return DEVICE_ASSIGNMENTS[device_id]

        if len(DEVICE_ASSIGNMENTS) >= len(SENSOR_ORDER):
            return None

        sensor_id = SENSOR_ORDER[len(DEVICE_ASSIGNMENTS)]
        DEVICE_ASSIGNMENTS[device_id] = sensor_id
        print(f"New device assigned: {device_id} -> {sensor_id}")
        return sensor_id


SENSOR_ALIASES = {
    "A": "S1",
    "L": "S1",
    "LEFT": "S1",
    "S1": "S1",
    "SENSOR_LEFT": "S1",
    "SENSOR_1": "S1",
    "B": "S2",
    "R": "S2",
    "RIGHT": "S2",
    "S2": "S2",
    "SENSOR_RIGHT": "S2",
    "SENSOR_2": "S2",
    "C": "S3",
    "BACK": "S3",
    "CENTER": "S3",
    "S3": "S3",
    "SENSOR_BACK": "S3",
    "SENSOR_CENTER": "S3",
    "SENSOR_3": "S3",
}

lock = Lock()
sensor_history: dict[str, list[dict[str, float | str]]] = {sensor: [] for sensor in SENSORS}


# ============================================================================
# Manager notifications
#
# Judgement runs entirely in memory, per device, using a fixed 60-slot
# circular buffer keyed by wall-clock second (epoch % 60). Every incoming
# sample updates at most one bucket (the "current second"), so cost per
# request is bounded by the fixed window size (60) and never grows with
# history length or database size - there is no DB query involved.
#
# Notification conditions (either one fires a notification):
#   (1) total red (>=75dB) seconds in the last 60s >= 15
#   (2) red (>=75dB) continuous streak >= 7 seconds
# ============================================================================

NOTIFY_WINDOW_SECONDS = 60
NOTIFY_TOTAL_RED_THRESHOLD = 15       # condition (1): total red seconds in the window
NOTIFY_CONTINUOUS_RED_THRESHOLD = 7   # condition (2): consecutive red seconds
NOTIFY_COOLDOWN_SECONDS = 10
NOTIFICATION_LOG_LIMIT = 200

NOTIFY_ZONE_NAMES = {
    "S1": "Zone A",
    "S2": "Zone B",
    "S3": "Zone C",
}

NOTIFY_REASON_TEXT = {
    "total": "Loud noise (>=75dB) totaled 15s+ within the last 60s",
    "continuous": "Loud noise (>=75dB) continued for 7s+ without a break",
}

notify_lock = Lock()
notification_log: list[dict[str, object]] = []
_next_notification_id = 1


class DeviceNotifyState:
    """Sliding-window red/green judgement for a single device.

    - `buckets[i]` holds the red flag for wall-clock second `i` (mod 60).
    - `red_total` is a running sum kept in sync with `buckets`, so the
      60-second total is read in O(1) instead of being recomputed.
    - `consecutive_red` counts the current unbroken streak of red seconds.
    """

    __slots__ = (
        "buckets",
        "red_total",
        "consecutive_red",
        "current_epoch",
        "cooldown_until",
    )

    def __init__(self) -> None:
        self.buckets = [0] * NOTIFY_WINDOW_SECONDS
        self.red_total = 0
        self.consecutive_red = 0
        self.current_epoch: int | None = None
        self.cooldown_until = 0.0

    def _reset(self) -> None:
        self.buckets = [0] * NOTIFY_WINDOW_SECONDS
        self.red_total = 0
        self.consecutive_red = 0

    def _clear_slot(self, epoch: int) -> None:
        slot = epoch % NOTIFY_WINDOW_SECONDS
        if self.buckets[slot]:
            self.red_total -= 1
        self.buckets[slot] = 0

    def _close_second(self, epoch: int) -> None:
        """Finalize the second that just elapsed into the streak counter."""
        slot = epoch % NOTIFY_WINDOW_SECONDS
        if not self.buckets[slot]:
            self.consecutive_red = 0

    def process_sample(self, db: float, now: float) -> str | None:
        """Fold one new sample into the window and return the notification
        reason ('total' | 'continuous') if a condition just fired and the
        device is not in cooldown, otherwise None."""
        epoch = int(now)
        is_red = db >= LOUD_MIN_DB

        if self.current_epoch is None:
            self.current_epoch = epoch
        elif epoch > self.current_epoch:
            gap = epoch - self.current_epoch
            if gap >= NOTIFY_WINDOW_SECONDS:
                # The device was silent/offline longer than the window covers;
                # nothing in the old window is still relevant.
                self._reset()
            else:
                self._close_second(self.current_epoch)
                for missed in range(self.current_epoch + 1, epoch):
                    self._clear_slot(missed)
                    self.consecutive_red = 0
            self._clear_slot(epoch)
            self.current_epoch = epoch

        slot = epoch % NOTIFY_WINDOW_SECONDS
        if is_red and not self.buckets[slot]:
            self.buckets[slot] = 1
            self.red_total += 1
            self.consecutive_red += 1

        reason = None
        if self.red_total >= NOTIFY_TOTAL_RED_THRESHOLD:
            reason = "total"
        if self.consecutive_red >= NOTIFY_CONTINUOUS_RED_THRESHOLD:
            reason = "continuous"

        if reason is None:
            return None
        if now < self.cooldown_until:
            return None

        self.cooldown_until = now + NOTIFY_COOLDOWN_SECONDS
        return reason


notify_states: dict[str, DeviceNotifyState] = {sensor: DeviceNotifyState() for sensor in SENSORS}


def record_notification(sensor: str, reason: str, now: float) -> dict[str, object]:
    global _next_notification_id

    zone_name = NOTIFY_ZONE_NAMES.get(sensor, sensor)
    entry = {
        "id": _next_notification_id,
        "sensor": sensor,
        "zone": zone_name,
        "reason": reason,
        "message": f"{zone_name} ({sensor}) - {NOTIFY_REASON_TEXT[reason]}",
        "timestamp": now,
        "time_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
    }

    with notify_lock:
        _next_notification_id += 1
        notification_log.insert(0, entry)
        del notification_log[NOTIFICATION_LOG_LIMIT:]

    print(f"[NOTIFY] {entry['time_text']} {entry['message']}")
    return entry


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def status_for(avg_db: float | None, last_seen_age: float | None = 0.0) -> str:
    if avg_db is None or last_seen_age is None or last_seen_age > OFFLINE_AFTER_SECONDS:
        return "OFFLINE"
    if avg_db >= LOUD_MIN_DB:
        return "RED"
    if avg_db >= QUIET_MAX_DB:
        return "YELLOW"
    return "QUIET"


def normalize_sensor(raw_value: object) -> str | None:
    value = str(raw_value or "").upper()
    return SENSOR_ALIASES.get(value)


def trim_history(sensor: str, now: float) -> None:
    sensor_history[sensor] = [
        item for item in sensor_history[sensor] if now - float(item["timestamp"]) <= WINDOW_SECONDS
    ]


def sensor_snapshot(sensor: str, now: float) -> dict[str, object]:
    trim_history(sensor, now)
    history = sensor_history[sensor]
    config = SENSORS[sensor]

    if not history:
        return {
            "sensor": sensor,
            "name": config["name"],
            "label": config["label"],
            "x_cm": config["x_cm"],
            "y_cm": config["y_cm"],
            "db": None,
            "avg_db": None,
            "status": "OFFLINE",
            "last_seen_age": None,
            "sample_count": 0,
        }

    latest = history[-1]
    values = [float(item["db"]) for item in history]
    avg_db = sum(values) / len(values)
    last_seen_age = now - float(latest["timestamp"])

    return {
        "sensor": sensor,
        "name": config["name"],
        "label": config["label"],
        "x_cm": config["x_cm"],
        "y_cm": config["y_cm"],
        "node_id": latest.get("node_id"),
        "db": round(float(latest["db"]), 1),
        "avg_db": round(avg_db, 1),
        "status": status_for(avg_db, last_seen_age),
        "last_seen_age": round(last_seen_age, 1),
        "sample_count": len(history),
    }


def map_x_percent(x_cm: float) -> float:
    return 12.0 + clamp(x_cm / TABLE_WIDTH_CM, 0.0, 1.0) * 76.0


def map_y_percent(y_cm: float) -> float:
    # y=0 is the front of the physical table, displayed near the bottom.
    return 78.0 - clamp(y_cm / TABLE_DEPTH_CM, 0.0, 1.0) * 56.0


def db_to_weight(db: float, max_db: float) -> float:
    # dB is logarithmic. This keeps strong sensors dominant without making
    # weaker sensors disappear completely on a small exhibition table.
    return math.pow(10.0, (db - max_db) / 12.0)


def nearest_sensor_label(x_cm: float, y_cm: float) -> str:
    nearest_key = "S1"
    nearest_distance = float("inf")

    for key, sensor in SENSORS.items():
        dx = x_cm - float(sensor["x_cm"])
        dy = y_cm - float(sensor["y_cm"])
        distance = math.sqrt(dx * dx + dy * dy)
        if distance < nearest_distance:
            nearest_distance = distance
            nearest_key = key

    return f"Near {SENSORS[nearest_key]['label']}"


def map_snapshot(sensors: dict[str, dict[str, object]]) -> dict[str, object]:
    active = [
        sensor
        for sensor in sensors.values()
        if sensor["avg_db"] is not None and sensor["status"] != "OFFLINE"
    ]

    if not active:
        return {
            "status": "OFFLINE",
            "overall_avg_db": None,
            "hotspot_x_percent": 50.0,
            "hotspot_y_percent": 50.0,
            "estimated_x_cm": None,
            "estimated_y_cm": None,
            "source_label": "No signal",
            "noisiest_zone": "No signal",
            "quietest_zone": "No signal",
            "confidence": 0.0,
            "active_sensor_count": 0,
        }

    max_db = max(float(sensor["avg_db"]) for sensor in active)
    min_db = min(float(sensor["avg_db"]) for sensor in active)
    noisiest_sensor = max(active, key=lambda sensor: float(sensor["avg_db"]))
    quietest_sensor = min(active, key=lambda sensor: float(sensor["avg_db"]))
    overall = max_db
    status = status_for(overall)

    weighted_x = 0.0
    weighted_y = 0.0
    total_weight = 0.0

    weights = []
    for sensor in active:
        weight = db_to_weight(float(sensor["avg_db"]), max_db)
        weights.append(weight)
        weighted_x += float(sensor["x_cm"]) * weight
        weighted_y += float(sensor["y_cm"]) * weight
        total_weight += weight

    x_cm = weighted_x / total_weight
    y_cm = weighted_y / total_weight

    if len(weights) == 1:
        confidence = 0.35
    else:
        sorted_weights = sorted(weights, reverse=True)
        confidence = clamp((sorted_weights[0] - sorted_weights[1]) / sorted_weights[0], 0.0, 1.0)

    return {
        "status": status,
        "overall_avg_db": round(overall, 1),
        "hotspot_x_percent": round(map_x_percent(x_cm), 1),
        "hotspot_y_percent": round(map_y_percent(y_cm), 1),
        "estimated_x_cm": round(x_cm),
        "estimated_y_cm": round(y_cm),
        "source_label": nearest_sensor_label(x_cm, y_cm),
        "noisiest_zone": f"{noisiest_sensor['name']} · {noisiest_sensor['label']}",
        "quietest_zone": f"{quietest_sensor['name']} · {quietest_sensor['label']}",
        "quietest_db": round(min_db, 1),
        "confidence": round(confidence, 2),
        "active_sensor_count": len(active),
    }


def all_state() -> dict[str, object]:
    now = time.time()
    with lock:
        sensors = {sensor: sensor_snapshot(sensor, now) for sensor in SENSORS}
        map_state = map_snapshot(sensors)

    return {
        "window_seconds": WINDOW_SECONDS,
        "table_width_cm": TABLE_WIDTH_CM,
        "table_depth_cm": TABLE_DEPTH_CM,
        "thresholds": {
            "green_below_db": QUIET_MAX_DB,
            "yellow_from_db": QUIET_MAX_DB,
            "red_from_db": LOUD_MIN_DB,
        },
        "sensors": sensors,
        "map": map_state,
        "table_device": {
            "table_id": "TABLE_1",
            "source": "overall map state",
        },
    }


class SilenceKeeperHandler(BaseHTTPRequestHandler):
    server_version = "SilenceKeeperHTTP/1.2"

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def send_body(self, status: int, body: bytes | str, content_type: str) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, data: dict[str, object]) -> None:
        self.send_body(status, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")

    def send_file(self, path: Path, content_type: str) -> None:
        if not path.exists() or not path.is_file():
            self.send_body(404, "Not found\n", "text/plain; charset=utf-8")
            return

        self.send_body(200, path.read_bytes(), content_type)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/":
            self.send_file(BASE_DIR / "templates" / "index.html", "text/html; charset=utf-8")
            return

        if path == "/static/style.css":
            self.send_file(BASE_DIR / "static" / "style.css", "text/css; charset=utf-8")
            return

        if path == "/api/state":
            self.send_json(200, all_state())
            return

        if path == "/api/notifications":
            with notify_lock:
                items = list(notification_log)
            self.send_json(200, {"notifications": items})
            return

        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "table" and parts[3] == "state.txt":
            self.handle_table_state()
            return

        self.send_body(404, "Not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = urlparse(self.path).path

        if path == "/api/reset-assignments":
            with assignment_lock:
                DEVICE_ASSIGNMENTS.clear()

            with lock:
                # Drop buffered readings too, so any still-connected device's old
                # data disappears immediately (zones show OFFLINE) instead of
                # lingering until it naturally ages out of the 10s window.
                for sensor in sensor_history:
                    sensor_history[sensor].clear()

            print("Device assignments reset")
            self.send_json(200, {"ok": True, "message": "Device assignments reset"})
            return

        if path == "/api/notifications/clear":
            with notify_lock:
                notification_log.clear()
            self.send_json(200, {"ok": True})
            return

        if path != "/api/noise":
            self.send_body(404, "Not found\n", "text/plain; charset=utf-8")
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8")

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            self.send_json(400, {"ok": False, "error": "Invalid JSON"})
            return

        # New ESP32 code sends a unique device_id (for example its MAC address).
        # The server assigns S1-S3 by the order in which unique devices first contact it.
        device_id = str(payload.get("device_id", "")).strip()

        if device_id:
            sensor = assign_sensor(device_id)
            if sensor is None:
                self.send_json(
                    409,
                    {"ok": False, "error": "All 3 sensor slots are already assigned"},
                )
                return
            node_id = device_id
        else:
            # Backward compatibility with the previous ESP32 payload format.
            sensor = normalize_sensor(payload.get("zone") or payload.get("sensor"))
            node_id = str(payload.get("node_id", "UNKNOWN"))

            if sensor is None:
                self.send_json(
                    400,
                    {"ok": False, "error": "device_id is required, or use a legacy S1-S3 zone"},
                )
                return

        try:
            db = float(payload.get("db"))
        except (TypeError, ValueError):
            self.send_json(400, {"ok": False, "error": "Invalid db"})
            return

        now = time.time()
        with lock:
            sensor_history[sensor].append(
                {
                    "timestamp": now,
                    "db": db,
                    "node_id": node_id,
                }
            )
            sensors = {item: sensor_snapshot(item, now) for item in SENSORS}
            snapshot = sensors[sensor]
            map_state = map_snapshot(sensors)
            notify_reason = notify_states[sensor].process_sample(db, now)

        if notify_reason:
            record_notification(sensor, notify_reason, now)

        self.send_json(200, {"ok": True, "sensor": sensor, "state": snapshot, "map": map_state})

    def handle_table_state(self) -> None:
        now = time.time()
        with lock:
            sensors = {sensor: sensor_snapshot(sensor, now) for sensor in SENSORS}
            map_state = map_snapshot(sensors)

        status = str(map_state["status"])
        overall_db = map_state["overall_avg_db"] if map_state["overall_avg_db"] is not None else 0.0
        source_label = str(map_state["source_label"]).replace(",", " ")
        self.send_body(200, f"{status},{overall_db},{source_label}\n", "text/plain; charset=utf-8")


def run() -> None:
    port = int(os.environ.get("PORT", 5000))
    server = ThreadingHTTPServer(("0.0.0.0", port), SilenceKeeperHandler)
    print("Silence Keeper server running")
    print(f"Dashboard: http://localhost:{port}")
    print("ESP32 URL: http://YOUR_LAPTOP_IP:%d" % port)
    server.serve_forever()


if __name__ == "__main__":
    run()
