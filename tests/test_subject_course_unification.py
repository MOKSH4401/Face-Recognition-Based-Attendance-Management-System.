"""
Tests verifying that Subject and Course are unified 1-to-1:
- Adding a Subject auto-creates / links a Course.
- Adding a Course auto-creates a Subject for timetable.
- Assigning a Timetable slot auto-assigns the Course to the Faculty for attendance.
- Faculty has instant course access from timetable entries.
"""
import unittest

from app import app, db
from models import (
    User, Faculty, Student, Course, Subject, Department, Semester,
    TimetableEntry, AttendanceSession, Attendance
)


class SubjectCourseUnificationTests(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

        # Clean DB
        TimetableEntry.query.delete()
        Attendance.query.delete()
        AttendanceSession.query.delete()
        Subject.query.delete()
        Semester.query.delete()
        Department.query.delete()
        Faculty.query.delete()
        Student.query.delete()
        Course.query.delete()
        User.query.delete()
        db.session.commit()
        db.session.expunge_all()

        # Common test setup
        self.dept = Department(name="Computer Engineering", code="CE")
        self.sem = Semester(name="Semester 4", code="SEM-4", is_active=True)
        db.session.add_all([self.dept, self.sem])
        db.session.commit()

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def test_add_subject_auto_creates_course(self):
        """Creating a Subject in Timetable automatically creates and links a Course."""
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['role'] = 'admin'

        res = self.client.post('/timetable', data={
            'action': 'add_subject',
            'subject_name': 'Operating Systems',
            'subject_code': 'OS401',
            'subject_department_id': str(self.dept.id),
            'subject_semester_id': str(self.sem.id),
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)

        # Subject exists
        sub = Subject.query.filter_by(code='OS401').first()
        self.assertIsNotNone(sub)
        self.assertEqual(sub.name, 'Operating Systems')

        # Course was automatically created and linked
        course = Course.query.filter_by(course_id='OS401').first()
        self.assertIsNotNone(course)
        self.assertEqual(course.name, 'Operating Systems')
        self.assertEqual(sub.course_id, course.id)
        self.assertEqual(course.subject.id, sub.id)

    def test_add_subject_links_to_existing_course(self):
        """If a Course already exists with same name/code, Subject auto-links to it."""
        course = Course(name="Database Systems", course_id="DBS301")
        db.session.add(course)
        db.session.commit()

        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['role'] = 'admin'

        res = self.client.post('/timetable', data={
            'action': 'add_subject',
            'subject_name': 'Database Systems',
            'subject_code': 'DBS301',
            'subject_department_id': str(self.dept.id),
            'subject_semester_id': str(self.sem.id),
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)

        sub = Subject.query.filter_by(code='DBS301').first()
        self.assertIsNotNone(sub)
        self.assertEqual(sub.course_id, course.id)
        # Should not create duplicate course
        self.assertEqual(Course.query.filter_by(course_id='DBS301').count(), 1)

    def test_add_course_auto_creates_subject(self):
        """Creating a Course in /courses automatically creates a matching Subject for Timetable."""
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['role'] = 'admin'

        res = self.client.post('/courses', data={
            'name': 'Computer Networks',
            'course_id': 'CN402',
            'classroom_lat': '22.6000',
            'classroom_lng': '72.8200',
            'classroom_radius_meters': '100'
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)

        course = Course.query.filter_by(course_id='CN402').first()
        self.assertIsNotNone(course)

        # Subject was auto-created
        sub = Subject.query.filter_by(code='CN402').first()
        self.assertIsNotNone(sub)
        self.assertEqual(sub.name, 'Computer Networks')
        self.assertEqual(sub.course_id, course.id)

    def test_assign_timetable_slot_auto_assigns_faculty_to_course(self):
        """Assigning a slot with a Subject to a Faculty automatically gives them course attendance access."""
        fac = Faculty(faculty_name="Prof. Sharma", faculty_id="F100", email="sharma@test.edu", password_hash="hash123", department="CE", designation="Assistant Professor")
        db.session.add(fac)
        db.session.commit()

        # Create Subject (which auto-creates Course)
        sub = Subject(name="Algorithms", code="ALGO501", department_id=self.dept.id, semester_id=self.sem.id)
        course = Course(name="Algorithms", course_id="ALGO501")
        db.session.add_all([sub, course])
        db.session.flush()
        sub.course_id = course.id
        db.session.commit()

        self.assertEqual(len(fac.courses), 0)

        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['role'] = 'admin'

        res = self.client.post('/timetable', data={
            'action': 'assign_slot',
            'slot_department_id': str(self.dept.id),
            'slot_semester_id': str(self.sem.id),
            'slot_subject_id': str(sub.id),
            'slot_faculty_id': str(fac.id),
            'slot_day': '1',  # Tuesday
            'slot_start': '10:00',
            'slot_end': '11:00',
            'slot_room': 'Lab-3'
        }, follow_redirects=True)

        self.assertEqual(res.status_code, 200)

        # Faculty now automatically has this course!
        db.session.refresh(fac)
        self.assertIn(course, fac.courses)

        # Faculty session check on /attendance
        with self.client.session_transaction() as sess:
            sess['user_id'] = 2
            sess['role'] = 'faculty'
            sess['faculty_db_id'] = fac.id

        res2 = self.client.get('/attendance')
        self.assertEqual(res2.status_code, 200)
        self.assertIn(b"Algorithms", res2.data)


if __name__ == '__main__':
    unittest.main()
