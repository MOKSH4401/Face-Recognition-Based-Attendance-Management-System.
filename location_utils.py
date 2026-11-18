"""Server-side GPS distance checks. Never trust a client locationVerified flag."""
from math import asin, cos, radians, sin, sqrt

from attendance_config import (
    CLASSROOM_RADIUS_METERS,
    GPS_ACCURACY_BUFFER_METERS,
    GPS_MAX_ACCURACY_METERS,
    get_classroom_radius,
    get_gps_accuracy_buffer,
    get_gps_max_accuracy,
)


def haversine_meters(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = radians(float(lat1)), radians(float(lat2))
    dp = radians(float(lat2) - float(lat1))
    dl = radians(float(lon2) - float(lon1))
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * r * asin(sqrt(min(1.0, a)))


def parse_coordinate(value, kind="coordinate"):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, f"Invalid {kind}."
    if kind == "latitude" and not -90 <= number <= 90:
        return None, "Latitude must be between -90 and 90."
    if kind == "longitude" and not -180 <= number <= 180:
        return None, "Longitude must be between -180 and 180."
    return number, None


def check_classroom_location(
    student_lat,
    student_lng,
    classroom_lat,
    classroom_lng,
    radius_meters=None,
    accuracy=None,
):
    """
    Return dict: ok, distance_m, allowed_m, message.
    Allowed radius = classroom radius + clamped GPS accuracy buffer.
    """
    default_radius = get_classroom_radius()
    radius = float(radius_meters if radius_meters is not None else default_radius)
    if radius <= 0:
        radius = default_radius

    max_acc = get_gps_max_accuracy()
    max_buffer = get_gps_accuracy_buffer()

    buffer = 0.0
    if accuracy is not None:
        try:
            acc = float(accuracy)
            if acc < 0:
                acc = 0.0
            if acc > max_acc:
                return {
                    "ok": False,
                    "code": "poor_gps_accuracy",
                    "distance_m": None,
                    "allowed_m": radius,
                    "accuracy_m": acc,
                    "message": (
                        f"GPS accuracy is too poor ({acc:.0f} m). "
                        "Move near a window or enable high-accuracy location."
                    ),
                }
            buffer = min(acc, max_buffer)
        except (TypeError, ValueError):
            buffer = 0.0

    if classroom_lat is None or classroom_lng is None:
        return {
            "ok": False,
            "code": "classroom_location_missing",
            "distance_m": None,
            "allowed_m": radius,
            "message": "This session has no classroom coordinates.",
        }

    allowed = radius + buffer
    distance = haversine_meters(student_lat, student_lng, classroom_lat, classroom_lng)
    if distance <= allowed:
        return {
            "ok": True,
            "distance_m": round(distance, 1),
            "allowed_m": round(allowed, 1),
            "accuracy_m": None if accuracy is None else float(accuracy),
            "message": f"Location verified ({distance:.0f} m from classroom).",
        }
    return {
        "ok": False,
        "code": "outside_classroom",
        "distance_m": round(distance, 1),
        "allowed_m": round(allowed, 1),
        "accuracy_m": None if accuracy is None else float(accuracy),
        "message": (
            f"You are {distance:.0f} m from the classroom. "
            f"Attendance is allowed only within {allowed:.0f} m."
        ),
    }
