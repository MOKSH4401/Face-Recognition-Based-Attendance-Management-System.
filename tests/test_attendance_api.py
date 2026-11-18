"""Flask API checks for PIN+GPS attendance using an in-memory SQLite database."""
from datetime import datetime, timedelta
import unittest

from pin_utils import current_pin, new_pin_secret


CLASSROOM = (28.6139, 77.2090)
FAR = (28.6300, 77.2090)


class AttendanceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import app, db
        from models import AttendanceSession, Course, Faculty, Student, User

        cls.app = app
        cls.db = db
        cls.AttendanceSession = AttendanceSession
        cls.Course = Course
        cls.Faculty = Faculty
        cls.Student = Student
        cls.User = User
        cls.client = app.test_client()
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        cls.db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        from models import (
            Attendance, AttendanceSession,
            faculty_course_association, student_course_association,
        )
        self.client = self.app.test_client()
        self.db.session.rollback()
        self.db.session.execute(student_course_association.delete())
        self.db.session.execute(faculty_course_association.delete())
        self.db.session.query(Attendance).delete()
        self.db.session.query(AttendanceSession).delete()
        self.db.session.query(self.Student).delete()
        self.db.session.query(self.Faculty).delete()
        self.db.session.query(self.Course).delete()
        self.db.session.query(self.User).delete()
        self.db.session.commit()
        self.db.session.expunge_all()

        self.course = self.Course(
            name="Physics",
            course_id="PHY101",
            classroom_lat=CLASSROOM[0],
            classroom_lng=CLASSROOM[1],
            classroom_radius_meters=80,
        )
        self.faculty = self.Faculty(
            faculty_id="F1",
            faculty_name="Ada",
            email="ada@test.edu",
            department="Science",
            designation="Lecturer",
            password_hash="x",
        )
        self.student = self.Student(name="Bob", student_id="S1", password_hash="x")
        self.other = self.Student(name="Cara", student_id="S2", password_hash="x")
        self.admin = self.User(
            username="admin",
            email="admin@test.edu",
            password_hash="x",
        )
        self.db.session.add_all([self.course, self.faculty, self.student, self.other, self.admin])
        self.db.session.flush()
        self.student.courses.append(self.course)
        self.faculty.courses.append(self.course)
        self.db.session.commit()

    def _login_student(self, student=None):
        student = student or self.student
        with self.client.session_transaction() as sess:
            sess["role"] = "student"
            sess["student_db_id"] = student.id

    def _login_faculty(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "faculty"
            sess["faculty_db_id"] = self.faculty.id

    def _login_admin(self):
        with self.client.session_transaction() as sess:
            sess["role"] = "admin"
            sess["user_id"] = self.admin.id

    def _open_session(self, minutes=15, active=True):
        now = datetime.utcnow()
        row = self.AttendanceSession(
            course_id=self.course.id,
            faculty_id=self.faculty.id,
            started_at=now,
            ends_at=now + timedelta(minutes=minutes) if minutes > 0 else now + timedelta(seconds=-5),
            is_active=active,
            classroom_lat=CLASSROOM[0],
            classroom_lng=CLASSROOM[1],
            radius_meters=80,
            pin_secret=new_pin_secret(),
        )
        self.db.session.add(row)
        self.db.session.commit()
        return row

    def _pin(self, row):
        return current_pin(row.pin_secret, row.id)[0]

    def _mark(self, row, pin=None, lat=None, lng=None, accuracy=12, extra=None):
        body = {
            "session_id": row.id,
            "pin": pin if pin is not None else self._pin(row),
            "latitude": CLASSROOM[0] if lat is None else lat,
            "longitude": CLASSROOM[1] if lng is None else lng,
            "accuracy": accuracy,
            "location_verified": True,
            "distance_ok": True,
        }
        if extra:
            body.update(extra)
        return self.client.post("/api/student/mark_attendance", json=body)

    def test_unauthenticated_mark_rejected(self):
        res = self.client.post("/api/student/mark_attendance", json={"pin": "123456"})
        self.assertEqual(res.status_code, 401)

    def test_unauthenticated_start_rejected(self):
        res = self.client.post("/api/sessions/start", json={"course_id": 1})
        self.assertEqual(res.status_code, 401)

    def test_student_cannot_read_pin(self):
        row = self._open_session()
        self._login_student()
        res = self.client.get(f"/api/sessions/{row.id}/pin")
        self.assertEqual(res.status_code, 401)
        self.assertNotIn("pin", res.get_json() or {})

    def test_faculty_can_read_pin(self):
        row = self._open_session()
        self._login_faculty()
        res = self.client.get(f"/api/sessions/{row.id}/pin")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(len(data["pin"]), 6)

    def test_valid_attendance_marked_present(self):
        row = self._open_session()
        self._login_student()
        res = self._mark(row)
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        from models import Attendance
        rec = Attendance.query.filter_by(session_id=row.id, student_id=self.student.id).one()
        self.assertEqual(rec.status, "present")
        self.assertEqual(rec.distance_meters, 0.0)

    def test_invalid_pin_rejected(self):
        row = self._open_session()
        self._login_student()
        res = self._mark(row, pin="000000")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["code"], "invalid_pin")

    def test_outside_radius_rejected(self):
        row = self._open_session()
        self._login_student()
        res = self._mark(row, lat=FAR[0], lng=FAR[1], accuracy=10)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["code"], "outside_classroom")

    def test_poor_gps_accuracy_rejected(self):
        row = self._open_session()
        self._login_student()
        res = self._mark(row, accuracy=500)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["code"], "poor_gps_accuracy")

    def test_duplicate_attendance_rejected(self):
        row = self._open_session()
        self._login_student()
        first = self._mark(row)
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        second = self._mark(row)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["code"], "duplicate")
        from models import Attendance
        self.assertEqual(Attendance.query.filter_by(session_id=row.id).count(), 1)

    def test_session_expiry_rejected(self):
        row = self._open_session(minutes=-1, active=True)
        self._login_student()
        res = self._mark(row)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["code"], "session_expired")

    def test_unenrolled_student_rejected(self):
        row = self._open_session()
        self._login_student(self.other)
        res = self._mark(row)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["code"], "wrong_class")

    def test_validate_pin_then_mark(self):
        row = self._open_session()
        self._login_student()
        pin = self._pin(row)
        chk = self.client.post("/api/student/validate_pin", json={
            "session_id": row.id,
            "pin": pin,
        })
        self.assertEqual(chk.status_code, 200, chk.get_data(as_text=True))
        res = self._mark(row, pin=pin)
        self.assertEqual(res.status_code, 200)

    def test_client_verification_flags_do_not_bypass_gps(self):
        row = self._open_session()
        self._login_student()
        res = self._mark(row, lat=FAR[0], lng=FAR[1], extra={"verified": True, "present": True})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["code"], "outside_classroom")

    def test_faculty_starts_session(self):
        self._login_faculty()
        res = self.client.post("/api/sessions/start", json={
            "course_id": self.course.id,
            "latitude": CLASSROOM[0],
            "longitude": CLASSROOM[1],
            "duration_minutes": 10,
            "radius_meters": 80,
        })
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        self.assertEqual(len(res.get_json()["session"]["pin"]), 6)

    def test_missing_location_rejected(self):
        row = self._open_session()
        self._login_student()
        pin = self._pin(row)
        res = self.client.post("/api/student/mark_attendance", json={
            "session_id": row.id,
            "pin": pin,
        })
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["code"], "location_missing")

    def test_concurrent_duplicate_submissions(self):
        import threading
        row = self._open_session()
        pin = self._pin(row)
        body = {
            "session_id": row.id,
            "pin": pin,
            "latitude": CLASSROOM[0],
            "longitude": CLASSROOM[1],
            "accuracy": 12,
        }
        clients = [self.app.test_client(), self.app.test_client()]
        for client in clients:
            with client.session_transaction() as sess:
                sess["role"] = "student"
                sess["student_db_id"] = self.student.id
        results = []

        def worker(client):
            results.append(client.post("/api/student/mark_attendance", json=body))

        threads = [threading.Thread(target=worker, args=(c,)) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        codes = sorted(r.status_code for r in results)
        self.assertEqual(codes, [200, 409])
        from models import Attendance
        self.assertEqual(Attendance.query.filter_by(session_id=row.id, student_id=self.student.id).count(), 1)

    def test_faculty_other_course_cannot_read_pin(self):
        other_faculty = self.Faculty(
            faculty_id="F2",
            faculty_name="Eve",
            email="eve@test.edu",
            department="Science",
            designation="Lecturer",
            password_hash="x",
        )
        self.db.session.add(other_faculty)
        self.db.session.commit()
        row = self._open_session()
        with self.client.session_transaction() as sess:
            sess["role"] = "faculty"
            sess["faculty_db_id"] = other_faculty.id
        res = self.client.get(f"/api/sessions/{row.id}/pin")
        self.assertEqual(res.status_code, 403)

    def test_realtime_update_reaches_faculty_socket(self):
        from live_attendance import socketio
        row = self._open_session()
        self._login_faculty()
        sio = socketio.test_client(self.app, flask_test_client=self.client)
        sio.emit("join_session", {"session_id": row.id})
        sio.get_received()
        self._login_student()
        mark = self._mark(row)
        self.assertEqual(mark.status_code, 200, mark.get_data(as_text=True))
        events = sio.get_received()
        names = [e["name"] for e in events]
        self.assertIn("attendance_update", names)
        sio.disconnect()


if __name__ == "__main__":
    unittest.main()
