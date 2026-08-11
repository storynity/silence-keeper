from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request

SERVER_URL = "http://localhost:5000/api/noise"
SENSORS = ("S1", "S2", "S3", "S4")


def post_noise(sensor: str, db: float) -> None:
    payload = json.dumps(
        {
            "node_id": f"DEMO_{sensor}",
            "zone": sensor,
            "db": round(db, 1),
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        SERVER_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=2) as response:
        response.read()


def demo_values(t: float) -> dict[str, float]:
    # A smooth moving demo source. Each sensor becomes louder in turn.
    angle = t * 0.75
    return {
        "S1": 45.0 + 10.0 * (0.5 + 0.5 * math.sin(angle)),
        "S2": 45.0 + 10.0 * (0.5 + 0.5 * math.sin(angle + 2.1)),
        "S3": 45.0 + 10.0 * (0.5 + 0.5 * math.sin(angle + 4.2)),
        "S4": 45.0 + 10.0 * (0.5 + 0.5 * math.sin(angle + 5.7)),
    }


def main() -> None:
    print("Silence Keeper demo sender started.")
    print("Open http://localhost:5000 in a browser.")
    print("Press Ctrl+C to stop.")

    start = time.time()

    while True:
        values = demo_values(time.time() - start)
        try:
            for sensor in SENSORS:
                post_noise(sensor, values[sensor])
            print(
                "sent "
                + ", ".join(f"{sensor}={values[sensor]:.1f}dB" for sensor in SENSORS)
            )
        except urllib.error.URLError:
            print("Server is not ready. Run START_SERVER.cmd first.")
        except TimeoutError:
            print("Server request timed out.")

        time.sleep(1.0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nDemo sender stopped.")
