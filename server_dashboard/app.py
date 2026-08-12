from __future__ import annotations

import json
import math
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent

WINDOW_SECONDS = 10
OFFLINE_AFTER_SECONDS = 8
QUIET_MAX_DB = 40.0
LOUD_MIN_DB = 50.0

# Exhibition table size. Adjust these to match the real display table.
TABLE_WIDTH_CM = 180
TABLE_DEPTH_CM = 80

# Sensor positions use physical table coordinates.
# x = distance from left, y = distance from front.
SENSORS = {
    "S1": {"name": "S1", "label": "Window-left", "x_cm": 25.0, "y_cm": 60.0},
    "S2": {"name": "S2", "label": "Window-right", "x_cm": 155.0, "y_cm": 60.0},
    "S3": {"name": "S3", "label": "Wall-left", "x_cm": 25.0, "y_cm": 20.0},
    "S4": {"name": "S4", "label": "Wall-right", "x_cm": 155.0, "y_cm": 20.0},
}

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
    "D": "S4",
    "S4": "S4",
    "SENSOR_RIGHT_BACK": "S4",
    "SENSOR_4": "S4",
}

lock = Lock()
sensor_history: dict[str, list[dict[str, float | str]]] = {sensor: [] for sensor in SENSORS}


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

        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "table" and parts[3] == "state.txt":
            self.handle_table_state()
            return

        self.send_body(404, "Not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = urlparse(self.path).path

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

        sensor = normalize_sensor(payload.get("zone") or payload.get("sensor"))
        node_id = str(payload.get("node_id", "UNKNOWN"))

        if sensor is None:
            self.send_json(400, {"ok": False, "error": "Unknown sensor. Use S1, S2, S3, or S4."})
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
    server = ThreadingHTTPServer(("0.0.0.0", 5000), SilenceKeeperHandler)
    print("Silence Keeper server running")
    print("Dashboard: http://localhost:5000")
    print("ESP32 URL: http://YOUR_LAPTOP_IP:5000")
    server.serve_forever()


if __name__ == "__main__":
    run()