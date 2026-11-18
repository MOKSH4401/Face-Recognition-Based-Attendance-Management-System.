import math
import time

from location_utils import check_classroom_location, haversine_meters, parse_coordinate
from pin_utils import current_pin, new_pin_secret, pin_for_window, verify_pin


def test_pin_is_six_digits_and_verifies():
    secret = new_pin_secret()
    pin, left, period = current_pin(secret, session_id=7)
    assert pin.isdigit() and len(pin) == 6
    assert 30 <= period <= 60
    assert 1 <= left <= period
    assert verify_pin(pin, secret, 7) is True
    assert verify_pin("000000", secret, 7) is False
    assert verify_pin("abc", secret, 7) is False
    assert verify_pin(pin, secret, 8) is False


def test_pin_accepts_previous_window():
    secret = new_pin_secret()
    now = time.time()
    period = 45
    window = int(now) // period
    previous = pin_for_window(secret, 3, window - 1)
    assert verify_pin(previous, secret, 3, at=now, period=period) is True


def test_pin_rejects_expired_two_windows_ago():
    secret = new_pin_secret()
    now = time.time()
    period = 45
    window = int(now) // period
    stale = pin_for_window(secret, 3, window - 2)
    assert verify_pin(stale, secret, 3, at=now, period=period) is False


def test_parse_coordinate_bounds():
    assert parse_coordinate("28.6", "latitude")[1] is None
    assert parse_coordinate("91", "latitude")[0] is None
    assert parse_coordinate("not-a-number", "longitude")[0] is None


def test_inside_classroom_radius():
    classroom = (28.6139, 77.2090)
    result = check_classroom_location(
        classroom[0], classroom[1],
        classroom[0], classroom[1],
        radius_meters=80,
        accuracy=15,
    )
    assert result["ok"] is True
    assert result["distance_m"] == 0.0


def test_outside_classroom_radius():
    classroom = (28.6139, 77.2090)
    far = (28.6300, 77.2090)
    distance = haversine_meters(far[0], far[1], classroom[0], classroom[1])
    assert distance > 200
    result = check_classroom_location(
        far[0], far[1],
        classroom[0], classroom[1],
        radius_meters=80,
        accuracy=10,
    )
    assert result["ok"] is False
    assert result["code"] == "outside_classroom"


def test_poor_gps_accuracy_rejected():
    classroom = (28.6139, 77.2090)
    result = check_classroom_location(
        classroom[0], classroom[1],
        classroom[0], classroom[1],
        radius_meters=80,
        accuracy=500,
    )
    assert result["ok"] is False
    assert result["code"] == "poor_gps_accuracy"


def test_client_flags_are_not_used_by_distance_math():
    # Server distance is independent of any client boolean.
    d = haversine_meters(0, 0, 0, 1)
    assert math.isclose(d, 111319, rel_tol=0.01)
