"""
Email Notification Module (Flask-Mail).
Handles SMTP config, defaulter warning emails, and send logs.
Does not modify PIN+GPS attendance calculation.
"""
import logging
import os
import threading
from datetime import datetime, date

from flask import current_app
from flask_mail import Mail, Message

logger = logging.getLogger(__name__)

mail = Mail()

# Avoid circular imports at module load — models/app helpers used inside functions


def format_mail_error(e, mail_server=''):
    """Generate user-friendly, actionable diagnostic message for email errors."""
    err = str(e)
    is_gmail = 'gmail' in str(mail_server).lower()

    if (
        'BadCredentials' in err
        or '535' in err
        or 'Username and Password not accepted' in err
        or ('Connection unexpectedly closed' in err and is_gmail)
    ):
        return (
            "SMTP Authentication Failed: Gmail rejected the username or password. "
            "For Gmail (smtp.gmail.com), you MUST generate and use a 16-character Google App Password "
            "(not your standard Google account password). Make sure 2-Step Verification is ON, "
            "go to https://myaccount.google.com/apppasswords, create an App Password for 'Mail', "
            "and save it in Email Settings."
        )
    if 'Connection unexpectedly closed' in err:
        return (
            "SMTP Connection unexpectedly closed by mail server. "
            "Please verify server host, port, TLS/SSL settings, and credentials."
        )
    if 'timed out' in err.lower() or 'timeout' in err.lower():
        return (
            "SMTP Connection timed out. "
            "The mail server took too long to respond. Please check host, port, or network firewall settings."
        )
    if 'Connection refused' in err or '10061' in err:
        return f"SMTP Connection refused by {mail_server or 'mail server'}. Please check if the mail server and port are accessible."
    if 'Recipient address rejected' in err or '550' in err:
        return f"Recipient email was rejected by the mail server: {err}"

    return f"Failed to send email: {err}"


def get_email_settings():
    from models import EmailSettings
    settings = EmailSettings.query.order_by(EmailSettings.id.asc()).first()
    if not settings:
        settings = EmailSettings(
            mail_server=os.environ.get('MAIL_SERVER', 'smtp.gmail.com'),
            mail_port=int(os.environ.get('MAIL_PORT', 587)),
            mail_use_tls=os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true',
            mail_use_ssl=os.environ.get('MAIL_USE_SSL', 'false').lower() == 'true',
            mail_username=os.environ.get('MAIL_USERNAME', ''),
            mail_password=os.environ.get('MAIL_PASSWORD', ''),
            mail_default_sender=os.environ.get('MAIL_DEFAULT_SENDER', '')
        )
        from app import db
        db.session.add(settings)
        db.session.commit()
    return settings


def apply_mail_config_from_db(settings=None):
    """Push EmailSettings into Flask app.config for Flask-Mail with .env fallback."""
    try:
        settings = settings or get_email_settings()
    except Exception as e:
        logger.warning(f"Could not load email settings: {e}")
        return

    app = current_app._get_current_object()
    mail_server = (settings.mail_server if settings else None) or os.environ.get('MAIL_SERVER') or 'smtp.gmail.com'
    mail_port = int((settings.mail_port if settings else None) or os.environ.get('MAIL_PORT') or 587)

    if settings and settings.mail_use_tls is not None:
        mail_use_tls = bool(settings.mail_use_tls)
    else:
        mail_use_tls = os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true'

    if settings and settings.mail_use_ssl is not None:
        mail_use_ssl = bool(settings.mail_use_ssl)
    else:
        mail_use_ssl = os.environ.get('MAIL_USE_SSL', 'false').lower() == 'true'

    mail_username = ((settings.mail_username if settings else None) or os.environ.get('MAIL_USERNAME') or '').strip()
    mail_password = (settings.mail_password if settings else None) or os.environ.get('MAIL_PASSWORD') or ''
    default_sender = (
        (settings.mail_default_sender if settings else None)
        or os.environ.get('MAIL_DEFAULT_SENDER')
        or mail_username
        or 'noreply@localhost'
    ).strip()

    app.config['MAIL_SERVER'] = mail_server
    app.config['MAIL_PORT'] = mail_port
    app.config['MAIL_USE_TLS'] = mail_use_tls
    app.config['MAIL_USE_SSL'] = mail_use_ssl
    app.config['MAIL_USERNAME'] = mail_username
    app.config['MAIL_PASSWORD'] = mail_password
    app.config['MAIL_DEFAULT_SENDER'] = default_sender
    # Re-bind mail state after config change
    mail.init_app(app)


def is_mail_configured(settings=None):
    try:
        settings = settings or get_email_settings()
    except Exception:
        settings = None

    server = (settings.mail_server if settings else None) or os.environ.get('MAIL_SERVER')
    port = (settings.mail_port if settings else None) or os.environ.get('MAIL_PORT')
    username = (settings.mail_username if settings else None) or os.environ.get('MAIL_USERNAME')
    password = (settings.mail_password if settings else None) or os.environ.get('MAIL_PASSWORD')
    sender = (settings.mail_default_sender if settings else None) or os.environ.get('MAIL_DEFAULT_SENDER') or username

    return bool(server and port and username and password and sender)


def build_defaulter_email_body(student_name, attendance_pct, course_name, threshold=75.0):
    warning = (
        f"WARNING: Your attendance has fallen below the required {threshold}% threshold. "
        f"Please attend classes regularly to avoid academic penalties."
    )
    return (
        f"Dear {student_name},\n\n"
        f"This is an attendance warning notification from the Attendance Management System.\n\n"
        f"Student Name: {student_name}\n"
        f"Course: {course_name}\n"
        f"Attendance %: {attendance_pct}%\n\n"
        f"Warning Message:\n{warning}\n\n"
        f"If you believe this is an error, please contact your faculty or the administration.\n\n"
        f"Regards,\n"
        f"Attendance Management System"
    )


def build_defaulter_email_html(student_name, attendance_pct, course_name, threshold=75.0):
    warning = (
        f"Your attendance has fallen below the required {threshold}% threshold. "
        f"Please attend classes regularly to avoid academic penalties."
    )
    return f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <h2 style="color: #dc3545;">Attendance Warning</h2>
      <p>Dear <strong>{student_name}</strong>,</p>
      <p>This is an attendance warning from the Attendance Management System.</p>
      <table style="border-collapse: collapse; width: 100%; margin: 16px 0;">
        <tr>
          <td style="padding: 8px; border: 1px solid #ddd;"><strong>Student Name</strong></td>
          <td style="padding: 8px; border: 1px solid #ddd;">{student_name}</td>
        </tr>
        <tr>
          <td style="padding: 8px; border: 1px solid #ddd;"><strong>Course</strong></td>
          <td style="padding: 8px; border: 1px solid #ddd;">{course_name}</td>
        </tr>
        <tr>
          <td style="padding: 8px; border: 1px solid #ddd;"><strong>Attendance %</strong></td>
          <td style="padding: 8px; border: 1px solid #ddd; color: #dc3545;"><strong>{attendance_pct}%</strong></td>
        </tr>
      </table>
      <div style="background: #fff3cd; border: 1px solid #ffecb5; padding: 12px; border-radius: 4px;">
        <strong>Warning Message:</strong>
        <p style="margin: 8px 0 0 0;">{warning}</p>
      </div>
      <p style="margin-top: 16px; color: #666; font-size: 13px;">
        If you believe this is an error, please contact your faculty or the administration.
      </p>
      <p>Regards,<br>Attendance Management System</p>
    </div>
    """


def _already_notified_today(student_id, course_id):
    from models import EmailNotificationLog
    from app import db
    today = date.today()
    existing = EmailNotificationLog.query.filter(
        EmailNotificationLog.student_id == student_id,
        EmailNotificationLog.course_id == course_id,
        EmailNotificationLog.status == 'sent',
        db.func.date(EmailNotificationLog.sent_at) == today
    ).first()
    return existing is not None


def send_defaulter_warning(student, course, attendance_percentage, trigger='manual', force=False):
    """
    Send one defaulter warning email.
    Returns dict: {ok, message, log_id}.
    """
    from app import db
    from models import EmailNotificationLog

    settings = get_email_settings()
    apply_mail_config_from_db(settings)

    recipient = (getattr(student, 'email', None) or '').strip()
    if not recipient:
        return {
            'ok': False,
            'message': f'No email address on file for {student.name}. Please add student email in Admin -> Manage Students.',
            'log_id': None
        }

    if not is_mail_configured(settings):
        return {
            'ok': False,
            'message': 'SMTP is not configured. Configure Mail Server, Username, and Password in Email Settings.',
            'log_id': None
        }

    if trigger == 'auto' and not settings.auto_notify_enabled:
        return {
            'ok': False,
            'message': 'Automatic email notifications are disabled.',
            'log_id': None
        }

    if trigger == 'auto' and not force and _already_notified_today(student.id, course.id):
        return {
            'ok': False,
            'message': f'Already notified {student.name} today for {course.name}.',
            'log_id': None,
            'skipped': True
        }

    threshold = float(settings.attendance_threshold or 75.0)
    pct = round(float(attendance_percentage), 2)
    subject = f"Attendance Warning — {course.name} ({pct}%)"
    body = build_defaulter_email_body(student.name, pct, course.name, threshold)
    html = build_defaulter_email_html(student.name, pct, course.name, threshold)

    log = EmailNotificationLog(
        student_id=student.id,
        course_id=course.id,
        recipient_email=recipient,
        subject=subject,
        attendance_percentage=pct,
        trigger=trigger,
        status='pending',
        sent_at=datetime.utcnow()
    )
    db.session.add(log)
    db.session.commit()

    try:
        msg = Message(
            subject=subject,
            recipients=[recipient],
            body=body,
            html=html
        )
        mail.send(msg)
        log.status = 'sent'
        log.error_message = None
        db.session.commit()
        logger.info(f"Defaulter email sent to {recipient} for student {student.id} course {course.id}")
        return {'ok': True, 'message': f'Email sent to {recipient}', 'log_id': log.id}
    except Exception as e:
        server_name = current_app.config.get('MAIL_SERVER', getattr(settings, 'mail_server', ''))
        err_msg = format_mail_error(e, server_name)
        log.status = 'failed'
        log.error_message = err_msg[:500]
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        logger.error(f"Failed to send defaulter email: {err_msg}")
        return {'ok': False, 'message': err_msg, 'log_id': log.id}


def notify_defaulters_for_course(course_id, trigger='auto'):
    """
    After a session is recorded, notify enrolled students who are now Defaulters.
    Safe to call from outside attendance logic; failures are logged, not raised.
    """
    from models import Student, Course

    try:
        from app import build_defaulter_records, DEFAULTER_THRESHOLD
    except Exception:
        from app import build_defaulter_records
        DEFAULTER_THRESHOLD = 75.0

    settings = get_email_settings()
    if trigger == 'auto' and not settings.auto_notify_enabled:
        return {'sent': 0, 'failed': 0, 'skipped': 0, 'results': []}

    threshold = float(settings.attendance_threshold or DEFAULTER_THRESHOLD)
    records = build_defaulter_records(
        course_ids=[int(course_id)],
        status_filter='Defaulter',
        threshold=threshold
    )

    results = []
    sent = failed = skipped = 0
    for rec in records:
        student = Student.query.get(rec['student_id'])
        course = Course.query.get(rec['course_id'])
        if not student or not course:
            continue
        if rec['attendance_percentage'] is None:
            continue
        result = send_defaulter_warning(
            student, course, rec['attendance_percentage'], trigger=trigger
        )
        results.append(result)
        if result.get('skipped'):
            skipped += 1
        elif result.get('ok'):
            sent += 1
        else:
            failed += 1

    return {'sent': sent, 'failed': failed, 'skipped': skipped, 'results': results}


def notify_all_defaulters(course_ids=None, trigger='manual', force=True):
    """Send warnings to all current defaulters (optional course scope)."""
    from models import Student, Course
    from app import build_defaulter_records, DEFAULTER_THRESHOLD

    settings = get_email_settings()
    threshold = float(settings.attendance_threshold or DEFAULTER_THRESHOLD)
    records = build_defaulter_records(
        course_ids=course_ids,
        status_filter='Defaulter',
        threshold=threshold
    )

    sent = failed = skipped = 0
    details = []
    for rec in records:
        student = Student.query.get(rec['student_id'])
        course = Course.query.get(rec['course_id'])
        if not student or not course:
            continue
        result = send_defaulter_warning(
            student, course, rec['attendance_percentage'],
            trigger=trigger, force=force
        )
        details.append({
            'student_name': student.name,
            'course_name': course.name,
            **result
        })
        if result.get('skipped'):
            skipped += 1
        elif result.get('ok'):
            sent += 1
        else:
            failed += 1

    return {'sent': sent, 'failed': failed, 'skipped': skipped, 'details': details}


def send_test_email(to_address):
    """Send a simple SMTP test message."""
    settings = get_email_settings()
    apply_mail_config_from_db(settings)
    if not is_mail_configured(settings):
        return {'ok': False, 'message': 'SMTP is not fully configured. Please configure server, username, and password.'}
    if not to_address:
        return {'ok': False, 'message': 'Recipient address is required.'}

    try:
        msg = Message(
            subject='Attendance System — SMTP Test',
            recipients=[to_address.strip()],
            body=(
                'This is a test email from the Attendance Management System.\n'
                'If you received this, SMTP settings are working correctly.'
            ),
            html=(
                '<p>This is a <strong>test email</strong> from the Attendance Management System.</p>'
                '<p>If you received this, SMTP settings are working correctly.</p>'
            )
        )
        mail.send(msg)
        return {'ok': True, 'message': f'Test email sent successfully to {to_address}'}
    except Exception as e:
        server_name = current_app.config.get('MAIL_SERVER', getattr(settings, 'mail_server', ''))
        err_msg = format_mail_error(e, server_name)
        logger.error(f"Test email failed: {err_msg}")
        return {'ok': False, 'message': err_msg}


# ---------------------------------------------------------------------------
# Auto-notify via SQLAlchemy events (does not alter attendance route logic)
# ---------------------------------------------------------------------------

_pending_notify_course_ids = set()
_hooks_registered = False


def _async_notify_courses(app, course_ids):
    """Run defaulter notifications safely in a background thread."""
    with app.app_context():
        for cid in course_ids:
            try:
                notify_defaulters_for_course(cid, trigger='auto')
            except Exception as e:
                logger.warning(f"Auto defaulter email failed for course {cid}: {e}")


def register_attendance_email_hooks(db):
    """
    When new Attendance rows are committed, automatically email students
    whose attendance for that course is below the configured threshold.
    """
    global _hooks_registered
    if _hooks_registered:
        return

    from sqlalchemy import event
    from models import Attendance

    @event.listens_for(Attendance, 'after_insert')
    def _queue_course_for_notify(mapper, connection, target):
        if target.course_id is not None:
            _pending_notify_course_ids.add(int(target.course_id))

    @event.listens_for(db.session, 'after_commit')
    def _notify_after_commit(session):
        course_ids = list(_pending_notify_course_ids)
        _pending_notify_course_ids.clear()
        if not course_ids:
            return

        try:
            app = current_app._get_current_object()
        except RuntimeError:
            app = None

        if app:
            t = threading.Thread(
                target=_async_notify_courses,
                args=(app, course_ids),
                daemon=True,
                name="EmailDefaulterNotifyThread"
            )
            t.start()
        else:
            for cid in course_ids:
                try:
                    notify_defaulters_for_course(cid, trigger='auto')
                except Exception as e:
                    logger.warning(f"Auto defaulter email failed for course {cid}: {e}")

    @event.listens_for(db.session, 'after_rollback')
    def _clear_pending_on_rollback(session):
        _pending_notify_course_ids.clear()

    _hooks_registered = True
    logger.info("Email auto-notify hooks registered on Attendance inserts")


def init_mail(app):
    """Attach Flask-Mail to the Flask app (call once at startup)."""
    mail.init_app(app)
    with app.app_context():
        apply_mail_config_from_db()
        try:
            from app import db
            register_attendance_email_hooks(db)
        except Exception as e:
            logger.warning(f"Could not register attendance email hooks: {e}")
