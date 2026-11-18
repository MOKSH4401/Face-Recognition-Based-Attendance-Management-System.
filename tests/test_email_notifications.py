"""
Tests for email notifications module (defaulter emails, SMTP diagnostics, configuration).
"""
import unittest
from unittest.mock import patch, MagicMock

from app import app, db
from models import User, Faculty, Student, Course, EmailSettings, EmailNotificationLog, AttendanceSession, Attendance
from email_service import (
    format_mail_error,
    is_mail_configured,
    build_defaulter_email_body,
    build_defaulter_email_html,
    send_defaulter_warning,
    notify_all_defaulters,
    notify_defaulters_for_course,
)


class EmailNotificationTests(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['MAIL_SUPPRESS_SEND'] = True
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()

        # Clean DB
        EmailNotificationLog.query.delete()
        EmailSettings.query.delete()
        Attendance.query.delete()
        AttendanceSession.query.delete()
        Student.query.delete()
        Course.query.delete()
        Faculty.query.delete()
        User.query.delete()
        db.session.commit()
        db.session.expunge_all()

    def tearDown(self):
        db.session.rollback()
        self.ctx.pop()

    def test_format_mail_error_gmail_credentials(self):
        err = "535-5.7.8 Username and Password not accepted. Go to https://support.google.com/mail/?p=BadCredentials"
        msg = format_mail_error(Exception(err), "smtp.gmail.com")
        self.assertIn("Google App Password", msg)
        self.assertIn("myaccount.google.com/apppasswords", msg)

    def test_format_mail_error_timeout(self):
        err = "The read operation timed out"
        msg = format_mail_error(Exception(err), "smtp.gmail.com")
        self.assertIn("timed out", msg.lower())

    def test_build_email_templates(self):
        body = build_defaulter_email_body("John Doe", 45.0, "Math 101", 75.0)
        self.assertIn("John Doe", body)
        self.assertIn("45.0%", body)
        self.assertIn("Math 101", body)

        html = build_defaulter_email_html("John Doe", 45.0, "Math 101", 75.0)
        self.assertIn("John Doe", html)
        self.assertIn("45.0%", html)
        self.assertIn("Math 101", html)

    def test_missing_student_email_rejected(self):
        student = Student(name="No Email Student", student_id="S001", email="")
        course = Course(name="Physics", course_id="PHY101")
        db.session.add_all([student, course])
        db.session.commit()

        res = send_defaulter_warning(student, course, 50.0, trigger='manual', force=True)
        self.assertFalse(res['ok'])
        self.assertIn("No email address on file", res['message'])

    def test_unconfigured_smtp_rejected(self):
        settings = EmailSettings(
            mail_server="",
            mail_username="",
            mail_password=""
        )
        db.session.add(settings)
        student = Student(name="Valid Student", student_id="S002", email="student@test.edu")
        course = Course(name="Chemistry", course_id="CHM101")
        db.session.add_all([student, course])
        db.session.commit()

        res = send_defaulter_warning(student, course, 50.0, trigger='manual', force=True)
        self.assertFalse(res['ok'])
        self.assertIn("SMTP is not configured", res['message'])

    @patch('email_service.mail.send')
    def test_successful_defaulter_email_logged(self, mock_send):
        settings = EmailSettings(
            mail_server="smtp.gmail.com",
            mail_port=587,
            mail_username="sender@test.edu",
            mail_password="app-password-test",
            mail_default_sender="sender@test.edu"
        )
        student = Student(name="Alice", student_id="S003", email="alice@test.edu")
        course = Course(name="Biology", course_id="BIO101")
        db.session.add_all([settings, student, course])
        db.session.commit()

        res = send_defaulter_warning(student, course, 60.0, trigger='manual', force=True)
        self.assertTrue(res['ok'])
        mock_send.assert_called_once()

        # Check DB log
        log = EmailNotificationLog.query.filter_by(student_id=student.id).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.status, 'sent')
        self.assertEqual(log.recipient_email, 'alice@test.edu')

    @patch('email_service.mail.send')
    def test_failed_defaulter_email_logged_with_friendly_message(self, mock_send):
        mock_send.side_effect = Exception("535 5.7.8 BadCredentials")

        settings = EmailSettings(
            mail_server="smtp.gmail.com",
            mail_port=587,
            mail_username="sender@test.edu",
            mail_password="bad-password",
            mail_default_sender="sender@test.edu"
        )
        student = Student(name="Bob", student_id="S004", email="bob@test.edu")
        course = Course(name="History", course_id="HIS101")
        db.session.add_all([settings, student, course])
        db.session.commit()

        res = send_defaulter_warning(student, course, 50.0, trigger='manual', force=True)
        self.assertFalse(res['ok'])
        self.assertIn("Google App Password", res['message'])

        log = EmailNotificationLog.query.filter_by(student_id=student.id).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.status, 'failed')
        self.assertIn("Google App Password", log.error_message)


if __name__ == '__main__':
    unittest.main()
