"""Classroom GPS and rotating-PIN settings. All verification happens on the server."""
import os
from dotenv import load_dotenv

load_dotenv()


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


CLASSROOM_RADIUS_METERS = _float("CLASSROOM_RADIUS_METERS", 100.0)
GPS_ACCURACY_BUFFER_METERS = _float("GPS_ACCURACY_BUFFER_METERS", 50.0)
GPS_MAX_ACCURACY_METERS = _float("GPS_MAX_ACCURACY_METERS", 200.0)
SESSION_DEFAULT_MINUTES = _int("SESSION_DEFAULT_MINUTES", 15)
PIN_PERIOD_SECONDS = _int("PIN_PERIOD_SECONDS", 45)

if PIN_PERIOD_SECONDS < 30:
    PIN_PERIOD_SECONDS = 30
if PIN_PERIOD_SECONDS > 60:
    PIN_PERIOD_SECONDS = 60


def get_gps_max_accuracy():
    return _float("GPS_MAX_ACCURACY_METERS", GPS_MAX_ACCURACY_METERS)


def get_gps_accuracy_buffer():
    return _float("GPS_ACCURACY_BUFFER_METERS", GPS_ACCURACY_BUFFER_METERS)


def get_classroom_radius():
    return _float("CLASSROOM_RADIUS_METERS", CLASSROOM_RADIUS_METERS)
