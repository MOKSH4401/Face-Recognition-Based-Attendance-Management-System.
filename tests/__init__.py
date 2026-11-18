"""Force SQLite before any test module imports the Flask app."""
import os

os.environ["ATTENDANCE_TESTING"] = "1"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["PIN_PERIOD_SECONDS"] = "45"
os.environ["CLASSROOM_RADIUS_METERS"] = "80"
os.environ["GPS_MAX_ACCURACY_METERS"] = "80"
os.environ["GPS_ACCURACY_BUFFER_METERS"] = "25"
