import os
import logging
from datetime import datetime, timedelta, date, time as dt_time
from calendar import month_abbr
from functools import wraps
from flask import Flask, render_template, redirect, url_for, request, jsonify, session, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from io import BytesIO
from dotenv import load_dotenv
from sqlalchemy.pool import StaticPool
from live_attendance import init_live_attendance, socketio
from attendance_config import CLASSROOM_RADIUS_METERS, PIN_PERIOD_SECONDS, SESSION_DEFAULT_MINUTES

load_dotenv()  # does not override env already set by pytest
# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Attendance below this threshold marks a student as a Defaulter
DEFAULTER_THRESHOLD = 75.0

class Base(DeclarativeBase):
    pass

# Initialize SQLAlchemy
db = SQLAlchemy(model_class=Base)

# Create Flask app
app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "default_secret_key")

# Tests set DATABASE_URL=sqlite:// so pytest never needs PostgreSQL.
pg_url = os.environ.get("DATABASE_URL")
if not pg_url:
    # If DATABASE_URL is not set, construct it from individual connection parameters
    pg_host = os.environ.get("PGHOST", "localhost") 
    pg_port = os.environ.get("PGPORT", "5432")
    pg_user = os.environ.get("PGUSER", "postgres")
    pg_password = os.environ.get("PGPASSWORD", "root")
    pg_database = os.environ.get("PGDATABASE", "postgres")
    
    pg_url = f"postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_database}"
    logger.info(f"Constructed PostgreSQL URL from environment variables (host: {pg_host})")
else:
    # Fix the URL format if needed
    if pg_url.startswith("postgres://"):
        pg_url = pg_url.replace("postgres://", "postgresql://", 1)
    logger.info("Using PostgreSQL URL from DATABASE_URL environment variable")

database_url = pg_url
if database_url.startswith("sqlite"):
    logger.info("Using SQLite (tests or local fallback)")
else:
    logger.info("PostgreSQL database configuration complete")

# Configure the application's database URI
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
logger.info(f"Database URL configured (without showing credential details)")

if database_url.startswith("sqlite"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False},
        "poolclass": StaticPool,
    }
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    @event.listens_for(Engine, "connect")
    def _sqlite_fk(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
else:
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_recycle": 300,
        "pool_pre_ping": True,
    }
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

# Default mail placeholders (overridden by Email Settings / DB)
app.config.setdefault('MAIL_SERVER', os.environ.get('MAIL_SERVER', 'smtp.gmail.com'))
app.config.setdefault('MAIL_PORT', int(os.environ.get('MAIL_PORT', 587)))
app.config.setdefault('MAIL_USE_TLS', os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true')
app.config.setdefault('MAIL_USE_SSL', os.environ.get('MAIL_USE_SSL', 'false').lower() == 'true')
app.config.setdefault('MAIL_USERNAME', os.environ.get('MAIL_USERNAME', ''))
app.config.setdefault('MAIL_PASSWORD', os.environ.get('MAIL_PASSWORD', ''))
app.config.setdefault('MAIL_DEFAULT_SENDER', os.environ.get('MAIL_DEFAULT_SENDER', ''))

# Initialize database with app
db.init_app(app)

@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({
        "status": "error",
        "message": "Request too large."
    }), 413

# Flask-Mail (Email Notification Module)
from email_service import init_mail, mail

# Initialize models and create tables within app context
with app.app_context():
    # Import models here to avoid circular imports
    from models import (
        User, Student, Course, Attendance, Faculty, AttendanceSession,
        faculty_course_association, student_course_association,
        EmailSettings, EmailNotificationLog,
        Notification, NotificationDelivery,
        Department, Semester, Subject, TimetableEntry
    )
    db.create_all()
    
    if db.engine.dialect.name == "postgresql":
        try:
            from sqlalchemy import text
            sql = text("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS reset_token VARCHAR(100),
            ADD COLUMN IF NOT EXISTS reset_token_expiry TIMESTAMP,
            ADD COLUMN IF NOT EXISTS security_question VARCHAR(100),
            ADD COLUMN IF NOT EXISTS security_answer VARCHAR(100)
            """)
            db.session.execute(sql)
            db.session.commit()
            logger.info("Added security question and reset token columns to users table")
        except Exception as e:
            logger.error(f"Error adding columns to users table: {str(e)}")
            db.session.rollback()

        try:
            from sqlalchemy import text
            db.session.execute(text(
                "ALTER TABLE students ADD COLUMN IF NOT EXISTS email VARCHAR(120)"
            ))
            db.session.execute(text(
                "ALTER TABLE students ADD COLUMN IF NOT EXISTS phone VARCHAR(20)"
            ))
            db.session.execute(text(
                "ALTER TABLE students ADD COLUMN IF NOT EXISTS password_hash VARCHAR(256)"
            ))
            db.session.commit()
            logger.info("Ensured students email/phone/password_hash columns exist")
        except Exception as e:
            logger.error(f"Error adding student portal columns: {str(e)}")
            db.session.rollback()

    if db.engine.dialect.name == "postgresql":
        try:
            from sqlalchemy import text
            for stmt in (
                "ALTER TABLE courses ADD COLUMN IF NOT EXISTS classroom_lat DOUBLE PRECISION",
                "ALTER TABLE courses ADD COLUMN IF NOT EXISTS classroom_lng DOUBLE PRECISION",
                "ALTER TABLE courses ADD COLUMN IF NOT EXISTS classroom_radius_meters DOUBLE PRECISION",
                "ALTER TABLE attendances ADD COLUMN IF NOT EXISTS session_id INTEGER",
                "ALTER TABLE attendances ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION",
                "ALTER TABLE attendances ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION",
                "ALTER TABLE attendances ADD COLUMN IF NOT EXISTS gps_accuracy DOUBLE PRECISION",
                "ALTER TABLE attendances ADD COLUMN IF NOT EXISTS distance_meters DOUBLE PRECISION",
                "ALTER TABLE attendances ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'present'",
                "ALTER TABLE attendance_sessions ADD COLUMN IF NOT EXISTS pin_secret VARCHAR(64)",
                "ALTER TABLE attendance_sessions ADD COLUMN IF NOT EXISTS subject_id INTEGER",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_attendance_session_student_notnull "
                "ON attendances (session_id, student_id) WHERE session_id IS NOT NULL",
            ):
                db.session.execute(text(stmt))
            try:
                db.session.execute(text("ALTER TABLE students DROP COLUMN IF EXISTS face_encoding"))
                db.session.execute(text("ALTER TABLE attendances DROP COLUMN IF EXISTS face_score"))
            except Exception as drop_err:
                logger.warning(f"Could not drop leftover face columns: {drop_err}")
            db.session.commit()
            logger.info("Ensured classroom GPS, PIN session, and unique attendance indexes exist")
        except Exception as e:
            logger.error(f"Error adding live-attendance columns: {str(e)}")
            db.session.rollback()

    # Initialize Flask-Mail from DB / env settings
    try:
        init_mail(app)
    except Exception as e:
        logger.warning(f"Flask-Mail init deferred: {str(e)}")

init_live_attendance(app, db)

with app.app_context():
    try:
        from models import Subject, Course, Department, Semester, Faculty, TimetableEntry

        # 1. Subject -> Course sync
        for sub in Subject.query.all():
            if not sub.course_id:
                course = Course.query.filter(
                    db.or_(
                        Course.course_id == sub.code,
                        db.func.lower(Course.name) == sub.name.lower()
                    )
                ).first()
                if not course:
                    course = Course(name=sub.name, course_id=sub.code)
                    db.session.add(course)
                    db.session.flush()
                sub.course_id = course.id
                logger.info(f"Auto-linked Subject '{sub.name}' to Course '{course.name}'")

        # 2. Course -> Subject sync
        dept = Department.query.first()
        sem = Semester.query.first()
        for c in Course.query.all():
            has_sub = Subject.query.filter_by(course_id=c.id).first()
            if not has_sub:
                existing_sub = Subject.query.filter(
                    db.or_(
                        Subject.code == c.course_id,
                        db.func.lower(Subject.name) == c.name.lower()
                    )
                ).first()
                if existing_sub:
                    existing_sub.course_id = c.id
                else:
                    if not dept:
                        dept = Department(name="General", code="GEN")
                        db.session.add(dept)
                        db.session.flush()
                    if not sem:
                        sem = Semester(name="Semester 1", code="SEM-1", is_active=True)
                        db.session.add(sem)
                        db.session.flush()
                    new_sub = Subject(
                        name=c.name,
                        code=c.course_id,
                        department_id=dept.id,
                        semester_id=sem.id,
                        course_id=c.id
                    )
                    db.session.add(new_sub)
                    logger.info(f"Auto-created Subject '{new_sub.name}' for Course '{c.name}'")

        # 3. Timetable slot -> Faculty.courses sync
        for entry in TimetableEntry.query.all():
            if entry.faculty and entry.subject and entry.subject.course_id:
                course = Course.query.get(entry.subject.course_id)
                if course and course not in entry.faculty.courses:
                    entry.faculty.courses.append(course)
                    logger.info(f"Auto-assigned Course '{course.name}' to Faculty '{entry.faculty.faculty_name}'")

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning(f"Subject-Course auto-sync warning: {e}")


def clear_session_auth():
    """Clear all auth-related session keys."""
    for key in (
        'user_id', 'username',
        'faculty_db_id', 'faculty_name', 'faculty_id_code',
        'student_db_id', 'student_name', 'student_code',
        'role'
    ):
        session.pop(key, None)


def admin_required(f):
    """Restrict route to logged-in admin users."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_admin():
            flash('Admin access required.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def faculty_required(f):
    """Restrict route to logged-in faculty members."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_faculty():
            flash('Faculty login required.', 'warning')
            return redirect(url_for('faculty_login'))
        return f(*args, **kwargs)
    return decorated


def student_required(f):
    """Restrict route to logged-in students."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_student():
            flash('Student login required.', 'warning')
            return redirect(url_for('student_login'))
        return f(*args, **kwargs)
    return decorated


def login_required(f):
    """Allow either admin or faculty sessions."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if is_admin() or is_faculty():
            return f(*args, **kwargs)
        flash('Please log in to continue.', 'warning')
        return redirect(url_for('login'))
    return decorated


def is_admin():
    if session.get('role') == 'admin' and 'user_id' in session:
        return True
    # Backward-compatible with sessions created before role was introduced
    if (
        'user_id' in session
        and 'faculty_db_id' not in session
        and 'student_db_id' not in session
    ):
        session['role'] = 'admin'
        return True
    return False


def is_faculty():
    return session.get('role') == 'faculty' and 'faculty_db_id' in session


def is_student():
    return session.get('role') == 'student' and 'student_db_id' in session


def get_current_faculty():
    if not is_faculty():
        return None
    return Faculty.query.get(session['faculty_db_id'])


def get_current_student():
    if not is_student():
        return None
    return Student.query.get(session['student_db_id'])


def get_accessible_courses():
    """Return courses visible to the current user (all for admin, assigned for faculty)."""
    if is_admin():
        return Course.query.order_by(Course.name).all()
    faculty = get_current_faculty()
    if faculty:
        courses_dict = {c.id: c for c in faculty.courses}
        # Also include any courses from faculty timetable entries
        for entry in faculty.timetable_entries:
            if entry.subject and entry.subject.course:
                courses_dict[entry.subject.course.id] = entry.subject.course
            elif entry.subject and entry.subject.course_id:
                c = Course.query.get(entry.subject.course_id)
                if c:
                    courses_dict[c.id] = c
        return sorted(courses_dict.values(), key=lambda c: c.name or '')
    return []


def faculty_has_course_access(course_id):
    """Check whether the current faculty member is assigned to a course."""
    if is_admin():
        return True
    faculty = get_current_faculty()
    if not faculty:
        return False
    try:
        cid = int(course_id)
    except (TypeError, ValueError):
        return False
    if any(c.id == cid for c in faculty.courses):
        return True
    for entry in faculty.timetable_entries:
        if entry.subject and entry.subject.course_id == cid:
            return True
    return False


def build_analytics_payload(course_ids=None):
    """
    Build analytics metrics and chart series from PostgreSQL.
    Read-only — does not modify attendance records.
    When course_ids is provided, metrics are scoped to those courses.
    """
    today = datetime.now().date()
    total_faculty = Faculty.query.count()

    # Empty faculty assignment set — return zeroed payload
    if course_ids is not None and len(course_ids) == 0:
        return {
            "summary": {
                "total_students": 0,
                "total_faculty": total_faculty,
                "total_courses": 0,
                "today_attendance": 0,
                "present_students": 0,
                "absent_students": 0,
                "attendance_percentage": 0.0,
                "weekly_attendance": 0,
                "monthly_attendance": 0,
            },
            "charts": {
                "weekly": {"labels": [], "values": []},
                "monthly": {"labels": [], "values": []},
                "present_absent": {"labels": ["Present", "Absent"], "values": [0, 0]},
                "by_course": {"labels": [], "values": []},
            },
            "generated_at": datetime.now().isoformat(),
        }

    attendance_base = Attendance.query
    if course_ids is not None:
        attendance_base = attendance_base.filter(Attendance.course_id.in_(course_ids))
        enrolled_students = Student.query.join(student_course_association).filter(
            student_course_association.c.course_id.in_(course_ids)
        ).distinct().count()
        total_students = enrolled_students
        total_courses = len(course_ids)
    else:
        total_students = Student.query.count()
        total_courses = Course.query.count()
        enrolled_students = total_students

    # Today's distinct present students and total marks
    today_records = attendance_base.filter(db.func.date(Attendance.timestamp) == today)
    today_attendance_count = today_records.count()
    present_rows = today_records.with_entities(Attendance.student_id).distinct().all()
    present_ids = {row[0] for row in present_rows}
    present_students = len(present_ids)
    absent_students = max(enrolled_students - present_students, 0)
    attendance_percentage = round(
        (present_students / enrolled_students) * 100, 1
    ) if enrolled_students else 0.0

    # Calendar week (Mon–Sun) and month totals
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    weekly_total = attendance_base.filter(
        db.func.date(Attendance.timestamp) >= week_start,
        db.func.date(Attendance.timestamp) <= today,
    ).count()
    monthly_total = attendance_base.filter(
        db.func.date(Attendance.timestamp) >= month_start,
        db.func.date(Attendance.timestamp) <= today,
    ).count()

    # Weekly series — last 7 days (bar chart)
    weekly_labels = []
    weekly_values = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        count = attendance_base.filter(db.func.date(Attendance.timestamp) == day).count()
        weekly_labels.append(day.strftime('%a %d'))
        weekly_values.append(count)

    # Monthly series — last 12 months (line chart)
    monthly_labels = []
    monthly_values = []
    year, month = today.year, today.month
    for offset in range(11, -1, -1):
        m = month - offset
        y = year
        while m <= 0:
            m += 12
            y -= 1
        start = date(y, m, 1)
        if m == 12:
            end = date(y + 1, 1, 1)
        else:
            end = date(y, m + 1, 1)
        count = attendance_base.filter(
            Attendance.timestamp >= datetime.combine(start, datetime.min.time()),
            Attendance.timestamp < datetime.combine(end, datetime.min.time()),
        ).count()
        monthly_labels.append(f"{month_abbr[m]} {y}")
        monthly_values.append(count)

    # Course breakdown — last 30 days
    thirty_ago = today - timedelta(days=29)
    course_query = db.session.query(
        Course.name,
        db.func.count(Attendance.id)
    ).outerjoin(
        Attendance,
        db.and_(
            Attendance.course_id == Course.id,
            db.func.date(Attendance.timestamp) >= thirty_ago
        )
    )
    if course_ids is not None:
        course_query = course_query.filter(Course.id.in_(course_ids))
    course_rows = course_query.group_by(Course.id, Course.name).order_by(Course.name).all()
    course_labels = [row[0] for row in course_rows]
    course_values = [row[1] for row in course_rows]

    return {
        "summary": {
            "total_students": total_students,
            "total_faculty": total_faculty,
            "total_courses": total_courses,
            "today_attendance": today_attendance_count,
            "present_students": present_students,
            "absent_students": absent_students,
            "attendance_percentage": attendance_percentage,
            "weekly_attendance": weekly_total,
            "monthly_attendance": monthly_total,
        },
        "charts": {
            "weekly": {"labels": weekly_labels, "values": weekly_values},
            "monthly": {"labels": monthly_labels, "values": monthly_values},
            "present_absent": {
                "labels": ["Present", "Absent"],
                "values": [present_students, absent_students],
            },
            "by_course": {"labels": course_labels, "values": course_values},
        },
        "generated_at": datetime.now().isoformat(),
    }


def build_admin_dashboard_feed(limit=8):
    """
    Recent lists + activity timeline for the redesigned admin dashboard.
    Read-only — does not alter attendance or other modules.
    """
    recent_students = Student.query.order_by(Student.created_at.desc()).limit(limit).all()
    recent_faculty = Faculty.query.order_by(Faculty.created_at.desc()).limit(limit).all()
    recent_attendance = (
        Attendance.query
        .order_by(Attendance.timestamp.desc())
        .limit(limit)
        .all()
    )

    students_payload = [{
        'id': s.id,
        'name': s.name,
        'student_id': s.student_id,
        'email': s.email or '',
        'courses': len(list(s.courses)) if s.courses is not None else 0,
        'created_at': s.created_at.strftime('%Y-%m-%d %H:%M') if s.created_at else '',
    } for s in recent_students]

    faculty_payload = [{
        'id': f.id,
        'name': f.faculty_name,
        'faculty_id': f.faculty_id,
        'department': f.department,
        'designation': f.designation,
        'email': f.email or '',
        'courses': len(list(f.courses)) if f.courses is not None else 0,
        'created_at': f.created_at.strftime('%Y-%m-%d %H:%M') if f.created_at else '',
    } for f in recent_faculty]

    attendance_payload = []
    for a in recent_attendance:
        attendance_payload.append({
            'id': a.id,
            'student_name': a.student.name if a.student else '—',
            'student_code': a.student.student_id if a.student else '',
            'course_name': a.course.name if a.course else '—',
            'course_code': a.course.course_id if a.course else '',
            'date': a.timestamp.strftime('%Y-%m-%d') if a.timestamp else '',
            'time': a.timestamp.strftime('%H:%M:%S') if a.timestamp else '',
            'timestamp': a.timestamp.isoformat() if a.timestamp else '',
        })

    activities = []
    for s in recent_students:
        activities.append({
            'type': 'student',
            'icon': 'fa-user-graduate',
            'color': 'primary',
            'title': f'New student registered: {s.name}',
            'meta': s.student_id,
            'at': s.created_at,
        })
    for f in recent_faculty:
        activities.append({
            'type': 'faculty',
            'icon': 'fa-chalkboard-teacher',
            'color': 'warning',
            'title': f'Faculty added: {f.faculty_name}',
            'meta': f.department,
            'at': f.created_at,
        })
    for a in recent_attendance:
        sname = a.student.name if a.student else 'Student'
        cname = a.course.name if a.course else 'Course'
        activities.append({
            'type': 'attendance',
            'icon': 'fa-clipboard-check',
            'color': 'success',
            'title': f'Attendance marked — {sname}',
            'meta': cname,
            'at': a.timestamp,
        })

    try:
        email_logs = EmailNotificationLog.query.order_by(
            EmailNotificationLog.sent_at.desc()
        ).limit(limit).all()
        for log in email_logs:
            activities.append({
                'type': 'email',
                'icon': 'fa-envelope',
                'color': 'info' if log.status == 'sent' else 'danger',
                'title': f'Email {log.status}: {log.recipient_email}',
                'meta': log.trigger or '',
                'at': log.sent_at,
            })
    except Exception:
        pass

    activities = [a for a in activities if a.get('at')]
    activities.sort(key=lambda x: x['at'], reverse=True)
    activities = activities[:12]
    for item in activities:
        item['at'] = item['at'].strftime('%Y-%m-%d %H:%M') if item['at'] else ''

    return {
        'recent_students': students_payload,
        'recent_faculty': faculty_payload,
        'recent_attendance': attendance_payload,
        'activities': activities,
    }


def get_course_session_counts(course_ids=None):
    """
    Count distinct session days per course (days when attendance was recorded).
    Returns {course_id: session_day_count}.
    """
    query = db.session.query(
        Attendance.course_id,
        db.func.count(db.func.distinct(db.func.date(Attendance.timestamp)))
    )
    if course_ids is not None:
        if not course_ids:
            return {}
        query = query.filter(Attendance.course_id.in_(course_ids))
    rows = query.group_by(Attendance.course_id).all()
    return {row[0]: row[1] for row in rows}


def get_student_present_counts(course_ids=None):
    """
    Count distinct present days per (student_id, course_id).
    Returns {(student_id, course_id): present_day_count}.
    """
    query = db.session.query(
        Attendance.student_id,
        Attendance.course_id,
        db.func.count(db.func.distinct(db.func.date(Attendance.timestamp)))
    )
    if course_ids is not None:
        if not course_ids:
            return {}
        query = query.filter(Attendance.course_id.in_(course_ids))
    rows = query.group_by(Attendance.student_id, Attendance.course_id).all()
    return {(row[0], row[1]): row[2] for row in rows}


def build_defaulter_records(course_ids=None, search=None, status_filter=None, threshold=None):
    """
    Calculate attendance % for every student–course enrollment and flag
    defaulters when percentage is below the threshold (default 75%).
    """
    if threshold is None:
        threshold = DEFAULTER_THRESHOLD

    # Enrollment pairs
    enroll_q = db.session.query(
        Student.id.label('student_pk'),
        Student.name.label('student_name'),
        Student.student_id.label('student_code'),
        Course.id.label('course_pk'),
        Course.name.label('course_name'),
        Course.course_id.label('course_code'),
    ).select_from(student_course_association).join(
        Student, Student.id == student_course_association.c.student_id
    ).join(
        Course, Course.id == student_course_association.c.course_id
    )

    if course_ids is not None:
        if not course_ids:
            return []
        enroll_q = enroll_q.filter(Course.id.in_(course_ids))

    if search:
        term = f"%{search.strip()}%"
        enroll_q = enroll_q.filter(
            db.or_(
                Student.name.ilike(term),
                Student.student_id.ilike(term),
                Course.name.ilike(term),
                Course.course_id.ilike(term),
            )
        )

    enrollments = enroll_q.order_by(Student.name, Course.name).all()
    session_counts = get_course_session_counts(course_ids)
    present_counts = get_student_present_counts(course_ids)

    records = []
    for row in enrollments:
        sessions = session_counts.get(row.course_pk, 0)
        present = present_counts.get((row.student_pk, row.course_pk), 0)

        if sessions <= 0:
            percentage = None
            status = 'No Data'
            reason = 'No attendance sessions recorded for this course yet.'
        else:
            percentage = round((present / sessions) * 100, 2)
            if percentage < threshold:
                status = 'Defaulter'
                reason = (
                    f'Attendance ({percentage}%) is below the required '
                    f'{threshold}% threshold ({present}/{sessions} sessions).'
                )
            else:
                status = 'Satisfactory'
                reason = (
                    f'Attendance ({percentage}%) meets the required '
                    f'{threshold}% threshold ({present}/{sessions} sessions).'
                )

        if status_filter and status_filter.lower() not in ('all', ''):
            if status.lower() != status_filter.lower():
                continue

        records.append({
            'student_id': row.student_pk,
            'student_name': row.student_name,
            'student_code': row.student_code,
            'course_id': row.course_pk,
            'course_name': row.course_name,
            'course_code': row.course_code,
            'present_sessions': present,
            'total_sessions': sessions,
            'attendance_percentage': percentage,
            'status': status,
            'reason': reason,
            'threshold': threshold,
        })

    return records


def build_defaulter_summary(records):
    """Aggregate KPI counts from computed defaulter records."""
    total = len(records)
    defaulters = sum(1 for r in records if r['status'] == 'Defaulter')
    satisfactory = sum(1 for r in records if r['status'] == 'Satisfactory')
    no_data = sum(1 for r in records if r['status'] == 'No Data')
    avg_pct = None
    scored = [r['attendance_percentage'] for r in records if r['attendance_percentage'] is not None]
    if scored:
        avg_pct = round(sum(scored) / len(scored), 2)
    return {
        'total_records': total,
        'defaulter_count': defaulters,
        'satisfactory_count': satisfactory,
        'no_data_count': no_data,
        'average_percentage': avg_pct,
        'threshold': DEFAULTER_THRESHOLD,
    }


def _accessible_course_ids_for_defaulters():
    if is_admin():
        return None  # all courses
    faculty = get_current_faculty()
    if not faculty:
        return []
    return [c.id for c in faculty.courses]


def _generate_defaulters_excel(records):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = Workbook()
    ws = wb.active
    ws.title = 'Defaulters'

    headers = [
        'Student Name', 'Student ID', 'Course', 'Course Code',
        'Present', 'Total Sessions', 'Attendance %', 'Status', 'Reason'
    ]
    header_fill = PatternFill('solid', fgColor='1F4E79')
    header_font = Font(color='FFFFFF', bold=True)
    thin = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )
    defaulter_fill = PatternFill('solid', fgColor='FFC7CE')

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center')
        cell.border = thin

    for row_idx, rec in enumerate(records, 2):
        pct = rec['attendance_percentage']
        values = [
            rec['student_name'],
            rec['student_code'],
            rec['course_name'],
            rec['course_code'],
            rec['present_sessions'],
            rec['total_sessions'],
            f"{pct}%" if pct is not None else 'N/A',
            rec['status'],
            rec['reason'],
        ]
        for col, value in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col, value=value)
            cell.border = thin
            if rec['status'] == 'Defaulter':
                cell.fill = defaulter_fill

    from openpyxl.utils import get_column_letter
    widths = [22, 14, 24, 12, 10, 14, 14, 14, 55]
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


def _generate_defaulters_pdf(records, summary):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=0.4 * inch,
        rightMargin=0.4 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'DefaulterTitle',
        parent=styles['Heading1'],
        fontSize=16,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        'DefaulterBody',
        parent=styles['Normal'],
        fontSize=8,
        leading=10,
    )

    story = []
    story.append(Paragraph('Defaulter Management Report', title_style))
    story.append(Paragraph(
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | "
        f"Threshold: {summary.get('threshold', DEFAULTER_THRESHOLD)}% | "
        f"Defaulters: {summary.get('defaulter_count', 0)} / {summary.get('total_records', 0)}",
        styles['Normal']
    ))
    story.append(Spacer(1, 0.2 * inch))

    data = [[
        Paragraph('<b>Student Name</b>', body_style),
        Paragraph('<b>Course</b>', body_style),
        Paragraph('<b>Attendance %</b>', body_style),
        Paragraph('<b>Status</b>', body_style),
        Paragraph('<b>Reason</b>', body_style),
    ]]

    for rec in records:
        pct = rec['attendance_percentage']
        pct_str = f"{pct}%" if pct is not None else 'N/A'
        data.append([
            Paragraph(f"{rec['student_name']}<br/><font size='7'>{rec['student_code']}</font>", body_style),
            Paragraph(f"{rec['course_name']}<br/><font size='7'>{rec['course_code']}</font>", body_style),
            Paragraph(pct_str, body_style),
            Paragraph(rec['status'], body_style),
            Paragraph(rec['reason'], body_style),
        ])

    if len(data) == 1:
        data.append([
            Paragraph('No records found.', body_style), '', '', '', ''
        ])

    table = Table(data, colWidths=[1.8 * inch, 1.8 * inch, 1.0 * inch, 1.0 * inch, 4.2 * inch])
    style_cmds = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1F4E79')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]
    for i, rec in enumerate(records, 1):
        if rec['status'] == 'Defaulter':
            style_cmds.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor('#FFC7CE')))
        elif rec['status'] == 'Satisfactory':
            style_cmds.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor('#C6EFCE')))

    table.setStyle(TableStyle(style_cmds))
    story.append(table)
    doc.build(story)
    buffer.seek(0)
    return buffer


# Routes
@app.route('/')
def index():
    if is_admin():
        return redirect(url_for('dashboard'))
    if is_faculty():
        return redirect(url_for('faculty_dashboard'))
    if is_student():
        return redirect(url_for('student_dashboard'))
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        user = User.query.filter_by(email=email).first()
        
        if not user or not check_password_hash(user.password_hash, password):
            error = 'Invalid email or password. Please try again.'
        else:
            clear_session_auth()
            session['user_id'] = user.id
            session['username'] = user.username
            session['role'] = 'admin'
            flash('Login successful!', 'success')
            return redirect(url_for('dashboard'))
    
    return render_template('login.html', error=error)

@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        security_question = request.form.get('security_question')
        security_answer = request.form.get('security_answer')
        
        # Validate email format
        import re
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_pattern, email):
            error = 'Invalid email format. Please enter a valid email address.'
            return render_template('register.html', error=error)
            
        # Validate password strength
        if len(password) < 8:
            error = 'Password must be at least 8 characters long.'
            return render_template('register.html', error=error)
            
        # Check for password complexity
        if not any(char.isdigit() for char in password):
            error = 'Password must contain at least one number.'
            return render_template('register.html', error=error)
            
        if not any(char.isupper() for char in password):
            error = 'Password must contain at least one uppercase letter.'
            return render_template('register.html', error=error)
            
        if not any(char.islower() for char in password):
            error = 'Password must contain at least one lowercase letter.'
            return render_template('register.html', error=error)
            
        if not any(char in '!@#$%^&*()-_=+[]{}|;:,.<>?/~`' for char in password):
            error = 'Password must contain at least one special character.'
            return render_template('register.html', error=error)
        
        # Validate security answer
        if not security_answer or len(security_answer.strip()) < 2:
            error = 'Security answer is required and must be at least 2 characters.'
            return render_template('register.html', error=error)
        
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            error = 'Email already registered. Please login or use a different email.'
        else:
            new_user = User(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
                security_question=security_question,
                security_answer=security_answer.lower()  # Store answer in lowercase for easier matching
            )
            try:
                db.session.add(new_user)
                db.session.commit()
                
                flash('Registration successful! Please login.', 'success')
                return redirect(url_for('login'))
            except Exception as e:
                logger.error(f"Error registering user: {str(e)}")
                db.session.rollback()
                error = f"Database error: {str(e)}"
    
    return render_template('register.html', error=error)
@app.route('/logout')
def logout():
    clear_session_auth()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))

@app.route('/dashboard')
@admin_required
def dashboard():
    """Admin analytics dashboard (metrics loaded via /api/analytics)."""
    return render_template('dashboard.html')


@app.route('/api/analytics')
def api_analytics():
    """
    JSON analytics API backed by PostgreSQL.
    Admin: system-wide + dashboard feed. Faculty: assigned courses only.
    Read-only — does not modify attendance logic.
    """
    if not is_admin() and not is_faculty():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    try:
        if is_faculty():
            faculty = get_current_faculty()
            course_ids = [c.id for c in faculty.courses] if faculty else []
            payload = build_analytics_payload(course_ids=course_ids)
        else:
            payload = build_analytics_payload(course_ids=None)
            payload['feed'] = build_admin_dashboard_feed(limit=8)
        return jsonify({"status": "success", "data": payload})
    except Exception as e:
        logger.error(f"Error building analytics: {str(e)}")
        return jsonify({
            "status": "error",
            "message": f"Could not load analytics: {str(e)}"
        }), 500


@app.route('/api/analytics/summary')
def api_analytics_summary():
    """KPI summary only."""
    if not is_admin() and not is_faculty():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    try:
        if is_faculty():
            faculty = get_current_faculty()
            course_ids = [c.id for c in faculty.courses] if faculty else []
            payload = build_analytics_payload(course_ids=course_ids)
        else:
            payload = build_analytics_payload(course_ids=None)
        return jsonify({"status": "success", "data": payload["summary"]})
    except Exception as e:
        logger.error(f"Error building analytics summary: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/students', methods=['GET', 'POST'])
@admin_required
def students():
    
    error = None
    if request.method == 'POST':
        name = request.form.get('name')
        student_id = request.form.get('student_id')
        email = (request.form.get('email') or '').strip() or None
        phone = (request.form.get('phone') or '').strip() or None
        portal_password = request.form.get('portal_password') or ''
        course_ids = request.form.getlist('course_ids')  # Note: we use getlist to capture multiple selections
        
        try:
            # Check if student already exists
            existing_student = Student.query.filter_by(student_id=student_id).first()
            if existing_student:
                flash('Student ID already exists!', 'danger')
            else:
                # Default portal password = Student ID if not provided
                pwd = portal_password.strip() if portal_password.strip() else student_id
                new_student = Student(
                    name=name,
                    student_id=student_id,
                    email=email,
                    phone=phone,
                    password_hash=generate_password_hash(pwd),
                )
                
                # Add the student to selected courses
                if course_ids:
                    courses = Course.query.filter(Course.id.in_(course_ids)).all()
                    new_student.courses = courses
                
                db.session.add(new_student)
                db.session.commit()
                flash(
                    f'Student added successfully! Portal login: Student ID + password '
                    f'({"custom" if portal_password.strip() else "same as Student ID"}).',
                    'success'
                )
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error adding student: {str(e)}")
            flash(f"Database error: {str(e)}", "danger")
    
    try:
        students = Student.query.all()
        courses = Course.query.all()
    except Exception as e:
        logger.error(f"Error retrieving students or courses: {str(e)}")
        students = []
        courses = []
        flash(f"Database error: {str(e)}", "danger")
    
    return render_template('students.html', students=students, courses=courses, error=error)
@app.route('/delete_student/<int:id>', methods=['POST'])
@admin_required
def delete_student(id):
    try:
        student = Student.query.get_or_404(id)
        db.session.delete(student)
        db.session.commit()
        flash('Student deleted successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting student: {str(e)}")
        flash(f"Database error: {str(e)}", "danger")
    
    return redirect(url_for('students'))

@app.route('/courses', methods=['GET', 'POST'])
@admin_required
def courses():
    error = None
    if request.method == 'POST':
        name = request.form.get('name')
        course_id = request.form.get('course_id')
        
        try:
            # Check if course already exists
            existing_course = Course.query.filter_by(course_id=course_id).first()
            if existing_course:
                flash('Course ID already exists!', 'danger')
            else:
                lat = request.form.get('classroom_lat')
                lng = request.form.get('classroom_lng')
                radius = request.form.get('classroom_radius_meters')
                new_course = Course(
                    name=name,
                    course_id=course_id,
                    classroom_lat=float(lat) if lat else None,
                    classroom_lng=float(lng) if lng else None,
                    classroom_radius_meters=float(radius) if radius else None,
                )
                db.session.add(new_course)
                db.session.flush()

                # Automatically create matching Subject for timetable
                dept = Department.query.first()
                if not dept:
                    dept = Department(name="General", code="GEN")
                    db.session.add(dept)
                    db.session.flush()
                sem = Semester.query.first()
                if not sem:
                    sem = Semester(name="Semester 1", code="SEM-1", is_active=True)
                    db.session.add(sem)
                    db.session.flush()

                new_sub = Subject(
                    name=name,
                    code=course_id,
                    department_id=dept.id,
                    semester_id=sem.id,
                    course_id=new_course.id
                )
                db.session.add(new_sub)
                db.session.commit()
                flash('Subject / Course added successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error adding course: {str(e)}")
            flash(f"Database error: {str(e)}", "danger")
    
    try:
        courses = Course.query.all()
    except Exception as e:
        logger.error(f"Error retrieving courses: {str(e)}")
        courses = []
        flash(f"Database error: {str(e)}", "danger")
    
    students = []  # Only for the template structure
    return render_template('students.html', courses=courses, students=students, active_tab='courses')

@app.route('/attendance')
@login_required
def attendance():
    try:
        courses = get_accessible_courses()
    except Exception as e:
        logger.error(f"Error retrieving courses for attendance: {str(e)}")
        courses = []
        flash(f"Database error: {str(e)}", "danger")

    current_slot = None
    try:
        current_slot = get_current_timetable_slot(
            faculty_id=session.get('faculty_db_id') if is_faculty() else None
        )
    except Exception as e:
        logger.warning(f"Timetable current slot lookup failed: {e}")

    selected_course_id = None
    if current_slot and current_slot.subject and current_slot.subject.course_id:
        cid = current_slot.subject.course_id
        if any(c.id == cid for c in courses) or is_admin():
            selected_course_id = cid

    subjects = []
    try:
        if courses:
            subjects = Subject.query.filter(
                Subject.course_id.in_([c.id for c in courses])
            ).order_by(Subject.name).all()
    except Exception as e:
        logger.warning(f"Could not load subjects for attendance: {e}")

    selected_subject_id = None
    if current_slot and current_slot.subject_id:
        selected_subject_id = current_slot.subject_id

    active_session_id = None
    target_course_id = selected_course_id or (courses[0].id if courses else None)
    if target_course_id:
        try:
            active_row = AttendanceSession.query.filter(
                AttendanceSession.course_id == target_course_id,
                AttendanceSession.is_active.is_(True),
                AttendanceSession.ends_at > datetime.utcnow()
            ).order_by(AttendanceSession.started_at.desc()).first()
            if active_row:
                active_session_id = active_row.id
        except Exception as e:
            logger.warning(f"Could not load active session for attendance: {e}")

    return render_template(
        'attendance.html',
        courses=courses,
        subjects=subjects,
        current_slot=current_slot,
        selected_course_id=selected_course_id,
        selected_subject_id=selected_subject_id,
        active_session_id=active_session_id,
        default_radius=CLASSROOM_RADIUS_METERS,
        default_duration=SESSION_DEFAULT_MINUTES,
        pin_period=PIN_PERIOD_SECONDS,
    )

@app.route('/reports')
@login_required
def reports():
    try:
        courses = get_accessible_courses()
        if is_faculty():
            assigned_course_ids = [c.id for c in courses]
            students = Student.query.join(student_course_association).filter(
                student_course_association.c.course_id.in_(assigned_course_ids)
            ).distinct().order_by(Student.name).all() if assigned_course_ids else []
            faculty_list = []
            departments = []
        else:
            students = Student.query.order_by(Student.name).all()
            faculty_list = Faculty.query.order_by(Faculty.faculty_name).all()
            departments = sorted({
                row[0] for row in db.session.query(Faculty.department).distinct().all()
                if row[0]
            })
    except Exception as e:
        logger.error(f"Error retrieving data for reports: {str(e)}")
        courses = []
        students = []
        faculty_list = []
        departments = []
        flash(f"Database error: {str(e)}", "danger")

    return render_template(
        'reports.html',
        courses=courses,
        students=students,
        faculty_list=faculty_list,
        departments=departments,
        is_admin_user=is_admin()
    )


def _course_ids_for_faculty(faculty_id):
    faculty = Faculty.query.get(int(faculty_id))
    if not faculty:
        return []
    return [c.id for c in faculty.courses]


def _course_ids_for_department(department):
    if not department:
        return []
    faculties = Faculty.query.filter(Faculty.department == department).all()
    course_ids = set()
    for f in faculties:
        for c in f.courses:
            course_ids.add(c.id)
    return list(course_ids)


def _faculty_dept_maps():
    """Map course_id -> (faculty names str, departments str)."""
    rows = db.session.query(
        faculty_course_association.c.course_id,
        Faculty.faculty_name,
        Faculty.department
    ).join(
        Faculty, Faculty.id == faculty_course_association.c.faculty_id
    ).all()

    names = {}
    depts = {}
    for course_id, fname, dept in rows:
        names.setdefault(course_id, [])
        depts.setdefault(course_id, [])
        if fname and fname not in names[course_id]:
            names[course_id].append(fname)
        if dept and dept not in depts[course_id]:
            depts[course_id].append(dept)
    return (
        {cid: ', '.join(sorted(v)) for cid, v in names.items()},
        {cid: ', '.join(sorted(v)) for cid, v in depts.items()},
    )


def query_attendance_report_records(args):
    """
    Shared filtered attendance query for JSON + exports.
    Supports: date_from, date_to, student_id, course_id, faculty_id, department.
    """
    course_id = args.get('course_id')
    student_id = args.get('student_id')
    date_from = args.get('date_from')
    date_to = args.get('date_to')
    faculty_id = args.get('faculty_id')
    department = args.get('department')

    query = db.session.query(
        Attendance,
        Student.name.label('student_name'),
        Student.student_id.label('student_code'),
        Course.id.label('course_pk'),
        Course.name.label('course_name'),
        Course.course_id.label('course_code'),
    ).select_from(Attendance).join(
        Student, Attendance.student_id == Student.id
    ).join(
        Course, Attendance.course_id == Course.id
    )

    # Role-based course scope
    if is_faculty():
        accessible = get_accessible_courses()
        accessible_ids = [c.id for c in accessible]
        if not accessible_ids:
            return []
        query = query.filter(Attendance.course_id.in_(accessible_ids))
        if course_id and course_id != 'all' and int(course_id) not in accessible_ids:
            raise PermissionError('Unauthorized course access')

    # Faculty filter → courses assigned to that faculty
    if is_admin() and faculty_id and faculty_id != 'all':
        f_course_ids = _course_ids_for_faculty(faculty_id)
        if not f_course_ids:
            return []
        query = query.filter(Attendance.course_id.in_(f_course_ids))

    # Department filter → courses taught by faculty in department
    if is_admin() and department and department != 'all':
        d_course_ids = _course_ids_for_department(department)
        if not d_course_ids:
            return []
        query = query.filter(Attendance.course_id.in_(d_course_ids))

    if course_id and course_id != 'all':
        query = query.filter(Attendance.course_id == int(course_id))

    if student_id and student_id != 'all':
        query = query.filter(Attendance.student_id == int(student_id))

    if date_from:
        query = query.filter(Attendance.timestamp >= date_from)

    if date_to:
        query = query.filter(Attendance.timestamp <= date_to + " 23:59:59")

    results = query.order_by(Attendance.timestamp.desc()).all()
    faculty_map, dept_map = _faculty_dept_maps()

    attendance_data = []
    for record in results:
        attendance = record[0]
        cid = record.course_pk
        attendance_data.append({
            'id': attendance.id,
            'student_name': record.student_name,
            'student_code': record.student_code,
            'course_name': record.course_name,
            'course_code': record.course_code,
            'faculty_names': faculty_map.get(cid, ''),
            'departments': dept_map.get(cid, ''),
            'timestamp': attendance.timestamp.isoformat(),
            'date': attendance.timestamp.strftime('%Y-%m-%d'),
            'time': attendance.timestamp.strftime('%H:%M:%S'),
        })
    return attendance_data


def _report_filter_labels(args):
    """Human-readable filter labels for export headers."""
    labels = {
        'date_from': args.get('date_from') or '',
        'date_to': args.get('date_to') or '',
        'course_label': 'All',
        'student_label': 'All',
        'faculty_label': 'All',
        'department_label': args.get('department') if args.get('department') not in (None, '', 'all') else 'All',
    }
    course_id = args.get('course_id')
    if course_id and course_id != 'all':
        c = Course.query.get(int(course_id))
        if c:
            labels['course_label'] = f"{c.name} ({c.course_id})"
    student_id = args.get('student_id')
    if student_id and student_id != 'all':
        s = Student.query.get(int(student_id))
        if s:
            labels['student_label'] = f"{s.name} ({s.student_id})"
    faculty_id = args.get('faculty_id')
    if faculty_id and faculty_id != 'all':
        f = Faculty.query.get(int(faculty_id))
        if f:
            labels['faculty_label'] = f"{f.faculty_name} ({f.department})"
    return labels


@app.route('/get_attendance_data', methods=['GET'])
def get_attendance_data():
    if not is_admin() and not is_faculty():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    try:
        data = query_attendance_report_records(request.args)
        return jsonify({"status": "success", "data": data, "count": len(data)})
    except PermissionError as e:
        return jsonify({"status": "error", "message": str(e)}), 403
    except Exception as e:
        logger.error(f"Error retrieving attendance data: {str(e)}")
        return jsonify({"status": "error", "message": f"An error occurred: {str(e)}"}), 500


@app.route('/api/reports/export/<fmt>')
@login_required
def api_reports_export(fmt):
    """Export filtered attendance as pdf | excel | csv."""
    fmt = (fmt or '').lower().strip()
    if fmt not in ('pdf', 'excel', 'csv', 'xlsx'):
        return jsonify({"status": "error", "message": "Unsupported format"}), 400
    if fmt == 'xlsx':
        fmt = 'excel'

    try:
        records = query_attendance_report_records(request.args)
        meta = _report_filter_labels(request.args)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')

        from report_service import (
            generate_attendance_pdf,
            generate_attendance_excel,
            generate_attendance_csv,
        )

        if fmt == 'pdf':
            buffer = generate_attendance_pdf(records, meta)
            return send_file(
                buffer,
                as_attachment=True,
                download_name=f'attendance_report_{stamp}.pdf',
                mimetype='application/pdf'
            )
        if fmt == 'excel':
            buffer = generate_attendance_excel(records, meta)
            return send_file(
                buffer,
                as_attachment=True,
                download_name=f'attendance_report_{stamp}.xlsx',
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
        buffer = generate_attendance_csv(records)
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f'attendance_report_{stamp}.csv',
            mimetype='text/csv'
        )
    except PermissionError as e:
        return jsonify({"status": "error", "message": str(e)}), 403
    except ImportError as e:
        logger.error(f"Report export missing dependency: {e}")
        return jsonify({
            "status": "error",
            "message": "Missing library. Install pandas, openpyxl, and reportlab."
        }), 500
    except Exception as e:
        logger.error(f"Error exporting report ({fmt}): {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Global Search Module
# ---------------------------------------------------------------------------

def _search_accessible_student_ids(course_ids):
    """Student PKs enrolled in any of the given courses."""
    if course_ids is None:
        return None
    if not course_ids:
        return []
    rows = db.session.query(student_course_association.c.student_id).filter(
        student_course_association.c.course_id.in_(course_ids)
    ).distinct().all()
    return [r[0] for r in rows]


def _parse_search_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def build_global_search_results(args):
    """
    Unified search across Student, Faculty, Course, Department, Attendance.
    Returns (results_page, total, meta) with sorting + pagination applied.
    """
    q = (args.get('q') or args.get('search') or '').strip()
    entity = (args.get('type') or args.get('entity') or 'all').strip().lower()
    if entity not in ('all', 'student', 'faculty', 'course', 'department', 'attendance'):
        entity = 'all'

    course_filter = args.get('course_id') or 'all'
    department_filter = (args.get('department') or '').strip()
    faculty_filter = args.get('faculty_id') or 'all'
    date_from = _parse_search_date(args.get('date_from'))
    date_to = _parse_search_date(args.get('date_to'))

    try:
        page = max(1, int(args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(args.get('per_page', 20))
    except (TypeError, ValueError):
        per_page = 20
    per_page = min(max(per_page, 5), 100)

    sort_by = (args.get('sort_by') or 'relevance').strip().lower()
    sort_dir = (args.get('sort_dir') or 'asc').strip().lower()
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'
    if sort_by not in ('relevance', 'title', 'type', 'department', 'date', 'id'):
        sort_by = 'relevance'

    # Role scope
    if is_faculty():
        accessible_courses = get_accessible_courses()
        accessible_ids = [c.id for c in accessible_courses]
        student_scope = _search_accessible_student_ids(accessible_ids)
        current_faculty = get_current_faculty()
    else:
        accessible_ids = None
        student_scope = None
        current_faculty = None

    # Narrow course filter relative to access
    if course_filter and course_filter != 'all':
        try:
            cid = int(course_filter)
        except (TypeError, ValueError):
            cid = None
        if cid is not None:
            if accessible_ids is not None and cid not in accessible_ids:
                raise PermissionError('Unauthorized course access')
            course_ids_for_query = [cid]
        else:
            course_ids_for_query = accessible_ids
    else:
        course_ids_for_query = accessible_ids

    if department_filter:
        dept_course_ids = _course_ids_for_department(department_filter)
        if course_ids_for_query is None:
            course_ids_for_query = dept_course_ids
        else:
            course_ids_for_query = [c for c in course_ids_for_query if c in set(dept_course_ids)]

    if is_admin() and faculty_filter and faculty_filter != 'all':
        f_ids = _course_ids_for_faculty(faculty_filter)
        if course_ids_for_query is None:
            course_ids_for_query = f_ids
        else:
            course_ids_for_query = [c for c in course_ids_for_query if c in set(f_ids)]

    term = f"%{q}%" if q else None
    results = []
    counts = {
        'student': 0, 'faculty': 0, 'course': 0,
        'department': 0, 'attendance': 0
    }

    # ---- Students ----
    if entity in ('all', 'student'):
        sq = Student.query
        if student_scope is not None:
            sq = sq.filter(Student.id.in_(student_scope or [-1]))
        if (
            (course_filter and course_filter != 'all')
            or department_filter
            or (is_admin() and faculty_filter and faculty_filter != 'all')
        ):
            if course_ids_for_query is not None:
                enrolled = _search_accessible_student_ids(course_ids_for_query)
                sq = sq.filter(Student.id.in_(enrolled or [-1]))
        if term:
            sq = sq.filter(db.or_(
                Student.name.ilike(term),
                Student.student_id.ilike(term),
                Student.email.ilike(term),
                Student.phone.ilike(term),
            ))
        for s in sq.order_by(Student.name).limit(500).all():
            course_names = ', '.join(c.name for c in s.courses) or '—'
            results.append({
                'type': 'student',
                'id': s.id,
                'title': s.name,
                'subtitle': f"ID: {s.student_id}",
                'meta': s.email or 'No email',
                'department': '',
                'date': s.created_at.strftime('%Y-%m-%d') if s.created_at else '',
                'date_sort': s.created_at.isoformat() if s.created_at else '',
                'extra': course_names,
                'link': url_for('edit_student', id=s.id) if is_admin() else url_for('reports'),
            })
        counts['student'] = len([r for r in results if r['type'] == 'student'])

    # ---- Faculty ----
    if entity in ('all', 'faculty') and (is_admin() or is_faculty()):
        fq = Faculty.query
        if is_faculty() and current_faculty:
            # Faculty sees colleagues in same department
            fq = fq.filter(Faculty.department == current_faculty.department)
        if department_filter:
            fq = fq.filter(Faculty.department == department_filter)
        if term:
            fq = fq.filter(db.or_(
                Faculty.faculty_name.ilike(term),
                Faculty.faculty_id.ilike(term),
                Faculty.email.ilike(term),
                Faculty.department.ilike(term),
                Faculty.designation.ilike(term),
            ))
        for f in fq.order_by(Faculty.faculty_name).limit(500).all():
            results.append({
                'type': 'faculty',
                'id': f.id,
                'title': f.faculty_name,
                'subtitle': f"ID: {f.faculty_id} · {f.designation}",
                'meta': f.email or '—',
                'department': f.department or '',
                'date': f.created_at.strftime('%Y-%m-%d') if f.created_at else '',
                'date_sort': f.created_at.isoformat() if f.created_at else '',
                'extra': f"{len(f.courses)} course(s)",
                'link': url_for('edit_faculty', id=f.id) if is_admin() else '',
            })
        counts['faculty'] = len([r for r in results if r['type'] == 'faculty'])

    # ---- Courses ----
    if entity in ('all', 'course'):
        cq = Course.query
        if course_ids_for_query is not None:
            cq = cq.filter(Course.id.in_(course_ids_for_query or [-1]))
        if term:
            cq = cq.filter(db.or_(
                Course.name.ilike(term),
                Course.course_id.ilike(term),
            ))
        fac_names, dept_map = _faculty_dept_maps()
        for c in cq.order_by(Course.name).limit(500).all():
            results.append({
                'type': 'course',
                'id': c.id,
                'title': c.name,
                'subtitle': f"Code: {c.course_id}",
                'meta': fac_names.get(c.id, 'Unassigned'),
                'department': dept_map.get(c.id, ''),
                'date': c.created_at.strftime('%Y-%m-%d') if c.created_at else '',
                'date_sort': c.created_at.isoformat() if c.created_at else '',
                'extra': f"{c.students.count()} student(s)",
                'link': url_for('reports') + f'?course_id={c.id}',
            })
        counts['course'] = len([r for r in results if r['type'] == 'course'])

    # ---- Departments (aggregated from faculty) ----
    if entity in ('all', 'department') and (is_admin() or is_faculty()):
        dq = db.session.query(
            Faculty.department,
            db.func.count(Faculty.id)
        )
        if is_faculty() and current_faculty:
            dq = dq.filter(Faculty.department == current_faculty.department)
        if department_filter:
            dq = dq.filter(Faculty.department == department_filter)
        if term:
            dq = dq.filter(Faculty.department.ilike(term))
        dq = dq.group_by(Faculty.department).order_by(Faculty.department)
        for dept_name, fac_count in dq.all():
            if not dept_name:
                continue
            cids = _course_ids_for_department(dept_name)
            if course_ids_for_query is not None and course_filter != 'all':
                if not set(cids).intersection(course_ids_for_query):
                    continue
            results.append({
                'type': 'department',
                'id': dept_name,
                'title': dept_name,
                'subtitle': f"{fac_count} faculty",
                'meta': f"{len(cids)} course(s)",
                'department': dept_name,
                'date': '',
                'date_sort': '',
                'extra': '',
                'link': url_for('reports') + f'?department={dept_name}',
            })
        counts['department'] = len([r for r in results if r['type'] == 'department'])

    # ---- Attendance (by date + related fields) ----
    if entity in ('all', 'attendance'):
        aq = db.session.query(
            Attendance,
            Student.name.label('student_name'),
            Student.student_id.label('student_code'),
            Course.name.label('course_name'),
            Course.course_id.label('course_code'),
        ).select_from(Attendance).join(
            Student, Attendance.student_id == Student.id
        ).join(
            Course, Attendance.course_id == Course.id
        )
        if course_ids_for_query is not None:
            aq = aq.filter(Attendance.course_id.in_(course_ids_for_query or [-1]))
        if date_from:
            aq = aq.filter(db.func.date(Attendance.timestamp) >= date_from)
        if date_to:
            aq = aq.filter(db.func.date(Attendance.timestamp) <= date_to)
        if term:
            aq = aq.filter(db.or_(
                Student.name.ilike(term),
                Student.student_id.ilike(term),
                Course.name.ilike(term),
                Course.course_id.ilike(term),
                db.func.to_char(Attendance.timestamp, 'YYYY-MM-DD').ilike(term),
            ))
        # If only date search with no text and entity is attendance, still return
        # If entity is all and no filters and no q, skip flooding attendance unless date set
        skip_attendance_flood = (
            entity == 'all'
            and not q
            and not date_from
            and not date_to
            and (not course_filter or course_filter == 'all')
            and not department_filter
            and (not faculty_filter or faculty_filter == 'all')
        )
        if not skip_attendance_flood:
            fac_names, dept_map = _faculty_dept_maps()
            for row in aq.order_by(Attendance.timestamp.desc()).limit(500).all():
                att = row[0]
                results.append({
                    'type': 'attendance',
                    'id': att.id,
                    'title': row.student_name,
                    'subtitle': f"{row.course_name} ({row.course_code})",
                    'meta': f"Student ID: {row.student_code}",
                    'department': dept_map.get(att.course_id, ''),
                    'date': att.timestamp.strftime('%Y-%m-%d %H:%M') if att.timestamp else '',
                    'date_sort': att.timestamp.isoformat() if att.timestamp else '',
                    'extra': fac_names.get(att.course_id, ''),
                    'link': url_for('reports'),
                })
        counts['attendance'] = len([r for r in results if r['type'] == 'attendance'])

    # Recount accurately (in case of multiple batches)
    for key in counts:
        counts[key] = sum(1 for r in results if r['type'] == key)

    # Sort
    type_order = {'student': 0, 'faculty': 1, 'course': 2, 'department': 3, 'attendance': 4}
    reverse = sort_dir == 'desc'

    def sort_key(item):
        if sort_by == 'title':
            return (item.get('title') or '').lower()
        if sort_by == 'type':
            return (type_order.get(item['type'], 99), (item.get('title') or '').lower())
        if sort_by == 'department':
            return ((item.get('department') or '').lower(), (item.get('title') or '').lower())
        if sort_by == 'date':
            return item.get('date_sort') or ''
        if sort_by == 'id':
            return str(item.get('id') or '')
        # relevance: prefer exact title match, then type order, then title
        title = (item.get('title') or '').lower()
        ql = q.lower() if q else ''
        exact = 0 if ql and title == ql else (1 if ql and ql in title else 2)
        return (exact, type_order.get(item['type'], 99), title)

    results.sort(key=sort_key, reverse=reverse)

    total = len(results)
    start = (page - 1) * per_page
    end = start + per_page
    page_rows = results[start:end]
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1

    meta = {
        'page': page,
        'per_page': per_page,
        'total': total,
        'total_pages': total_pages,
        'sort_by': sort_by,
        'sort_dir': sort_dir,
        'counts': counts,
        'q': q,
        'type': entity,
    }
    return page_rows, total, meta


@app.route('/search')
@login_required
def global_search():
    """Global search page — students, faculty, courses, departments, attendance."""
    courses = get_accessible_courses()
    faculty_list = []
    departments = []
    if is_admin():
        faculty_list = Faculty.query.order_by(Faculty.faculty_name).all()
        departments = sorted({
            row[0] for row in db.session.query(Faculty.department).distinct().all()
            if row[0]
        })
    elif is_faculty():
        _, dept_map = _faculty_dept_maps()
        course_ids = [c.id for c in courses]
        departments = sorted({
            d for cid in course_ids for d in (dept_map.get(cid) or '').split(', ') if d
        })
        fac = get_current_faculty()
        if fac and fac.department and fac.department not in departments:
            departments.append(fac.department)
            departments.sort()
        faculty_list = []

    return render_template(
        'search.html',
        courses=courses,
        faculty_list=faculty_list,
        departments=departments,
        is_admin_user=is_admin(),
        initial_q=request.args.get('q', ''),
        initial_type=request.args.get('type', 'all'),
        initial_date_from=request.args.get('date_from', ''),
        initial_date_to=request.args.get('date_to', ''),
    )


@app.route('/api/search')
def api_global_search():
    """JSON global search with filters, sorting, and pagination."""
    if not is_admin() and not is_faculty():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    try:
        rows, total, meta = build_global_search_results(request.args)
        return jsonify({
            "status": "success",
            "results": rows,
            "meta": meta,
        })
    except PermissionError as e:
        return jsonify({"status": "error", "message": str(e)}), 403
    except Exception as e:
        logger.error(f"Global search error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Defaulter Management Module
# ---------------------------------------------------------------------------

@app.route('/defaulters')
@login_required
def defaulters():
    """Defaulter dashboard — Bootstrap UI with search, filter, and export."""
    courses = get_accessible_courses()
    return render_template(
        'defaulters.html',
        courses=courses,
        threshold=DEFAULTER_THRESHOLD
    )


@app.route('/api/defaulters')
def api_defaulters():
    """
    JSON API: attendance % per student–course enrollment.
    Query params: search, course_id, status (all|Defaulter|Satisfactory|No Data)
    """
    if not is_admin() and not is_faculty():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    try:
        search = request.args.get('search', '').strip() or None
        course_id = request.args.get('course_id', 'all')
        status_filter = request.args.get('status', 'all')

        accessible = _accessible_course_ids_for_defaulters()
        if accessible is not None:
            if course_id and course_id != 'all':
                cid = int(course_id)
                if cid not in accessible:
                    return jsonify({"status": "error", "message": "Unauthorized course access"}), 403
                course_ids = [cid]
            else:
                course_ids = accessible
        else:
            if course_id and course_id != 'all':
                course_ids = [int(course_id)]
            else:
                course_ids = None

        # For summary KPIs, compute all statuses then filter display list
        all_records = build_defaulter_records(
            course_ids=course_ids,
            search=search,
            status_filter=None
        )
        summary = build_defaulter_summary(all_records)

        if status_filter and status_filter.lower() not in ('all', ''):
            records = [r for r in all_records if r['status'].lower() == status_filter.lower()]
        else:
            records = all_records

        # Default list view focuses on defaulters unless status explicitly set
        # Keep 'all' as full list when requested; UI defaults status to Defaulter

        return jsonify({
            "status": "success",
            "data": {
                "records": records,
                "summary": summary,
                "threshold": DEFAULTER_THRESHOLD,
                "generated_at": datetime.now().isoformat(),
            }
        })
    except Exception as e:
        logger.error(f"Error in api_defaulters: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/defaulters/export/excel')
def api_defaulters_export_excel():
    """Export filtered defaulter report as Excel (.xlsx)."""
    if not is_admin() and not is_faculty():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    try:
        search = request.args.get('search', '').strip() or None
        course_id = request.args.get('course_id', 'all')
        status_filter = request.args.get('status', 'all')

        accessible = _accessible_course_ids_for_defaulters()
        if accessible is not None:
            if course_id and course_id != 'all':
                cid = int(course_id)
                if cid not in accessible:
                    return jsonify({"status": "error", "message": "Unauthorized course access"}), 403
                course_ids = [cid]
            else:
                course_ids = accessible
        else:
            course_ids = [int(course_id)] if course_id and course_id != 'all' else None

        records = build_defaulter_records(
            course_ids=course_ids,
            search=search,
            status_filter=status_filter
        )
        buffer = _generate_defaulters_excel(records)
        filename = f"defaulters_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        return send_file(
            buffer,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
    except ImportError:
        logger.error("openpyxl is not installed")
        return jsonify({
            "status": "error",
            "message": "Excel export requires openpyxl. Install with: pip install openpyxl"
        }), 500
    except Exception as e:
        logger.error(f"Error exporting defaulters excel: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/defaulters/export/pdf')
def api_defaulters_export_pdf():
    """Export filtered defaulter report as PDF."""
    if not is_admin() and not is_faculty():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    try:
        search = request.args.get('search', '').strip() or None
        course_id = request.args.get('course_id', 'all')
        status_filter = request.args.get('status', 'all')

        accessible = _accessible_course_ids_for_defaulters()
        if accessible is not None:
            if course_id and course_id != 'all':
                cid = int(course_id)
                if cid not in accessible:
                    return jsonify({"status": "error", "message": "Unauthorized course access"}), 403
                course_ids = [cid]
            else:
                course_ids = accessible
        else:
            course_ids = [int(course_id)] if course_id and course_id != 'all' else None

        records = build_defaulter_records(
            course_ids=course_ids,
            search=search,
            status_filter=status_filter
        )
        summary = build_defaulter_summary(records)
        buffer = _generate_defaulters_pdf(records, summary)
        filename = f"defaulters_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        return send_file(
            buffer,
            as_attachment=True,
            download_name=filename,
            mimetype='application/pdf'
        )
    except ImportError:
        logger.error("reportlab is not installed")
        return jsonify({
            "status": "error",
            "message": "PDF export requires reportlab. Install with: pip install reportlab"
        }), 500
    except Exception as e:
        logger.error(f"Error exporting defaulters pdf: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# In-App Notification Module
# ---------------------------------------------------------------------------

PRIORITY_CHOICES = ('info', 'warning', 'urgent')
AUDIENCE_CHOICES = ('all', 'faculty', 'students', 'course')


def _resolve_notification_recipients(audience, course_id=None):
    """
    Build list of (role, faculty_id|None, student_id|None) for delivery rows.
    """
    recipients = []
    seen_f = set()
    seen_s = set()

    def add_faculty(fids):
        for fid in fids:
            if fid and fid not in seen_f:
                seen_f.add(fid)
                recipients.append(('faculty', fid, None))

    def add_students(sids):
        for sid in sids:
            if sid and sid not in seen_s:
                seen_s.add(sid)
                recipients.append(('student', None, sid))

    if audience == 'faculty':
        add_faculty([f.id for f in Faculty.query.all()])
    elif audience == 'students':
        add_students([s.id for s in Student.query.all()])
    elif audience == 'course':
        course = Course.query.get(course_id) if course_id else None
        if not course:
            return []
        add_faculty([f.id for f in course.faculty_members.all()])
        add_students([s.id for s in course.students.all()])
    else:  # all
        add_faculty([f.id for f in Faculty.query.all()])
        add_students([s.id for s in Student.query.all()])

    return recipients


def create_notification_broadcast(title, message, priority='info', audience='all',
                                  course_id=None, created_by=None):
    """Persist notification + per-recipient deliveries. Returns (notification, count)."""
    priority = priority if priority in PRIORITY_CHOICES else 'info'
    audience = audience if audience in AUDIENCE_CHOICES else 'all'
    if audience == 'course' and not course_id:
        raise ValueError('Course is required for course audience.')

    recipients = _resolve_notification_recipients(audience, course_id)
    if not recipients:
        raise ValueError('No recipients found for the selected audience.')

    notif = Notification(
        title=title.strip(),
        message=message.strip(),
        priority=priority,
        audience=audience,
        course_id=int(course_id) if course_id and audience == 'course' else None,
        created_by=created_by,
        is_active=True,
    )
    db.session.add(notif)
    db.session.flush()

    for role, fid, sid in recipients:
        db.session.add(NotificationDelivery(
            notification_id=notif.id,
            recipient_role=role,
            faculty_id=fid,
            student_id=sid,
            is_read=False,
        ))
    db.session.commit()
    return notif, len(recipients)


def get_current_user_unread_count():
    """Unread deliveries for the logged-in faculty or student."""
    if is_faculty():
        return NotificationDelivery.query.filter_by(
            faculty_id=session.get('faculty_db_id'),
            is_read=False
        ).count()
    if is_student():
        return NotificationDelivery.query.filter_by(
            student_id=session.get('student_db_id'),
            is_read=False
        ).count()
    return 0


def get_inbox_deliveries(only_unread=False, limit=100):
    """Deliveries for current faculty/student inbox."""
    q = None
    if is_faculty():
        q = NotificationDelivery.query.filter_by(faculty_id=session.get('faculty_db_id'))
    elif is_student():
        q = NotificationDelivery.query.filter_by(student_id=session.get('student_db_id'))
    else:
        return []
    q = q.join(Notification).filter(Notification.is_active.is_(True))
    if only_unread:
        q = q.filter(NotificationDelivery.is_read.is_(False))
    return q.order_by(NotificationDelivery.created_at.desc()).limit(limit).all()


@app.context_processor
def inject_notification_badge():
    """Expose unread_notification_count to all templates."""
    try:
        if is_faculty() or is_student():
            return {'unread_notification_count': get_current_user_unread_count()}
    except Exception:
        pass
    return {'unread_notification_count': 0}


@app.route('/notifications', methods=['GET', 'POST'])
def notifications_page():
    """
    Admin: create notifications + history.
    Faculty / Student: Bootstrap card inbox + history.
    """
    if is_admin():
        if request.method == 'POST':
            title = (request.form.get('title') or '').strip()
            message = (request.form.get('message') or '').strip()
            priority = request.form.get('priority', 'info')
            audience = request.form.get('audience', 'all')
            course_id = request.form.get('course_id') or None
            if not title or not message:
                flash('Title and message are required.', 'danger')
                return redirect(url_for('notifications_page'))
            try:
                notif, count = create_notification_broadcast(
                    title=title,
                    message=message,
                    priority=priority,
                    audience=audience,
                    course_id=course_id,
                    created_by=session.get('user_id'),
                )
                flash(
                    f'Notification "{notif.title}" sent to {count} recipient(s).',
                    'success'
                )
            except ValueError as e:
                flash(str(e), 'warning')
            except Exception as e:
                db.session.rollback()
                logger.error(f"Create notification failed: {e}")
                flash(f'Failed to create notification: {e}', 'danger')
            return redirect(url_for('notifications_page'))

        history = Notification.query.order_by(Notification.created_at.desc()).limit(100).all()
        courses = Course.query.order_by(Course.name).all()
        return render_template(
            'notifications_admin.html',
            history=history,
            courses=courses,
            priorities=PRIORITY_CHOICES,
            audiences=AUDIENCE_CHOICES,
        )

    if is_faculty() or is_student():
        deliveries = get_inbox_deliveries(limit=100)
        unread = sum(1 for d in deliveries if not d.is_read)
        return render_template(
            'notifications_inbox.html',
            deliveries=deliveries,
            unread_count=unread,
            role='faculty' if is_faculty() else 'student',
        )

    flash('Please log in to view notifications.', 'warning')
    return redirect(url_for('login'))


@app.route('/notifications/<int:delivery_id>/read', methods=['POST'])
def notification_mark_read(delivery_id):
    if not is_faculty() and not is_student():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    delivery = NotificationDelivery.query.get_or_404(delivery_id)
    if is_faculty() and delivery.faculty_id != session.get('faculty_db_id'):
        return jsonify({"status": "error", "message": "Forbidden"}), 403
    if is_student() and delivery.student_id != session.get('student_db_id'):
        return jsonify({"status": "error", "message": "Forbidden"}), 403
    if not delivery.is_read:
        delivery.is_read = True
        delivery.read_at = datetime.utcnow()
        db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or \
            request.accept_mimetypes.best == 'application/json':
        return jsonify({
            "status": "success",
            "unread": get_current_user_unread_count()
        })
    return redirect(url_for('notifications_page'))


@app.route('/notifications/read-all', methods=['POST'])
def notification_mark_all_read():
    if not is_faculty() and not is_student():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    q = NotificationDelivery.query.filter_by(is_read=False)
    if is_faculty():
        q = q.filter_by(faculty_id=session.get('faculty_db_id'))
    else:
        q = q.filter_by(student_id=session.get('student_db_id'))
    now = datetime.utcnow()
    for d in q.all():
        d.is_read = True
        d.read_at = now
    db.session.commit()
    flash('All notifications marked as read.', 'success')
    return redirect(url_for('notifications_page'))


@app.route('/notifications/<int:notif_id>/delete', methods=['POST'])
@admin_required
def notification_delete(notif_id):
    notif = Notification.query.get_or_404(notif_id)
    db.session.delete(notif)
    db.session.commit()
    flash('Notification deleted from history.', 'info')
    return redirect(url_for('notifications_page'))


@app.route('/api/notifications/unread-count')
def api_notification_unread_count():
    if not is_faculty() and not is_student():
        return jsonify({"status": "error", "message": "Unauthorized", "count": 0}), 401
    return jsonify({"status": "success", "count": get_current_user_unread_count()})


# ---------------------------------------------------------------------------
# Email Notification Module
# ---------------------------------------------------------------------------

@app.route('/email_settings', methods=['GET', 'POST'])
@admin_required
def email_settings():
    """Admin page to configure SMTP and automatic defaulter emails."""
    from email_service import (
        get_email_settings, apply_mail_config_from_db, is_mail_configured
    )

    settings = get_email_settings()

    if request.method == 'POST':
        try:
            settings.mail_server = (request.form.get('mail_server') or '').strip()
            settings.mail_port = int(request.form.get('mail_port') or 587)
            settings.mail_use_tls = request.form.get('mail_use_tls') == 'on'
            settings.mail_use_ssl = request.form.get('mail_use_ssl') == 'on'
            settings.mail_username = (request.form.get('mail_username') or '').strip()
            new_password = request.form.get('mail_password', '')
            # Keep existing password if field left blank
            if new_password:
                settings.mail_password = new_password
            settings.mail_default_sender = (request.form.get('mail_default_sender') or '').strip()
            settings.auto_notify_enabled = request.form.get('auto_notify_enabled') == 'on'
            try:
                settings.attendance_threshold = float(request.form.get('attendance_threshold') or 75)
            except ValueError:
                settings.attendance_threshold = 75.0
            settings.updated_at = datetime.utcnow()
            db.session.commit()
            apply_mail_config_from_db(settings)
            flash('Email settings saved successfully.', 'success')
            return redirect(url_for('email_settings'))
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error saving email settings: {e}")
            flash(f'Could not save settings: {e}', 'danger')

    recent_logs = EmailNotificationLog.query.order_by(
        EmailNotificationLog.sent_at.desc()
    ).limit(25).all()

    return render_template(
        'email_settings.html',
        settings=settings,
        configured=is_mail_configured(settings),
        recent_logs=recent_logs
    )


@app.route('/api/email/test', methods=['POST'])
@admin_required
def api_email_test():
    """Send a test email using current SMTP settings."""
    from email_service import send_test_email
    data = request.get_json(silent=True) or {}
    to_address = data.get('to') or request.form.get('to') or ''
    if not to_address:
        settings = None
        try:
            from email_service import get_email_settings
            settings = get_email_settings()
            to_address = settings.mail_username or ''
        except Exception:
            pass
    result = send_test_email(to_address)
    code = 200 if result.get('ok') else 400
    return jsonify({"status": "success" if result.get('ok') else "error", **result}), code


@app.route('/api/email/send-defaulter', methods=['POST'])
@login_required
def api_email_send_defaulter():
    """Manually send a warning email for one student–course pair."""
    from email_service import send_defaulter_warning

    data = request.get_json(silent=True) or {}
    student_id = data.get('student_id') or request.form.get('student_id')
    course_id = data.get('course_id') or request.form.get('course_id')

    if not student_id or not course_id:
        return jsonify({"status": "error", "message": "student_id and course_id are required"}), 400

    try:
        student_id = int(student_id)
        course_id = int(course_id)
    except ValueError:
        return jsonify({"status": "error", "message": "Invalid IDs"}), 400

    if not faculty_has_course_access(course_id):
        return jsonify({"status": "error", "message": "Unauthorized course access"}), 403

    student = Student.query.get(student_id)
    course = Course.query.get(course_id)
    if not student or not course:
        return jsonify({"status": "error", "message": "Student or course not found"}), 404

    records = build_defaulter_records(course_ids=[course_id], status_filter=None)
    match = next((r for r in records if r['student_id'] == student_id), None)
    if not match or match['attendance_percentage'] is None:
        return jsonify({
            "status": "error",
            "message": "No attendance data available for this enrollment."
        }), 400

    # Manual send allowed even if above threshold (admin override), but warn if OK
    result = send_defaulter_warning(
        student, course, match['attendance_percentage'],
        trigger='manual', force=True
    )
    return jsonify({
        "status": "success" if result.get('ok') else "error",
        "attendance_percentage": match['attendance_percentage'],
        "defaulter_status": match['status'],
        **result
    }), (200 if result.get('ok') else 400)


@app.route('/api/email/send-all-defaulters', methods=['POST'])
@admin_required
def api_email_send_all_defaulters():
    """Manually email all current defaulters (optional course filter)."""
    from email_service import notify_all_defaulters

    data = request.get_json(silent=True) or {}
    course_id = data.get('course_id') or request.form.get('course_id')
    course_ids = None
    if course_id and course_id != 'all':
        try:
            course_ids = [int(course_id)]
        except ValueError:
            return jsonify({"status": "error", "message": "Invalid course_id"}), 400

    result = notify_all_defaulters(course_ids=course_ids, trigger='manual', force=True)
    msg = f"Sent: {result['sent']}, Failed: {result['failed']}, Skipped: {result['skipped']}."
    if result['failed'] > 0 and result.get('details'):
        failure_messages = [d.get('message') for d in result['details'] if not d.get('ok') and d.get('message')]
        if failure_messages:
            msg += f"\n\nReason: {failure_messages[0]}"
    status = "success" if result['failed'] == 0 else "error" if result['sent'] == 0 else "warning"
    return jsonify({
        "status": status,
        "message": msg,
        **result
    })


@app.route('/health')
def health_check():
    """Health check endpoint for monitoring"""
    try:
        # Check database connectivity
        db.session.execute("SELECT 1")
        
        # All checks passed
        return jsonify({
            "status": "healthy",
            "database": "connected",
            "message": "All systems operational"
        })
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return jsonify({
            "status": "unhealthy",
            "database": "disconnected",
            "message": f"System error: {str(e)}"
        }), 500

@app.route('/dbinfo')
@admin_required
def db_info():
    """Database information endpoint"""
    try:
        # Get database version
        version_query = db.session.execute("SELECT version()").fetchone()
        version = version_query[0] if version_query else "Unknown"
        
        # Get table counts
        user_count = User.query.count()
        faculty_count = Faculty.query.count()
        student_count = Student.query.count()
        course_count = Course.query.count()
        attendance_count = Attendance.query.count()
        
        # Get database connection info (safely)
        connection_info = app.config["SQLALCHEMY_DATABASE_URI"]
        # Mask password for security
        if '@' in connection_info:
            connection_info = connection_info.split('@')
            auth_part = connection_info[0].split(':')
            if len(auth_part) > 2:  # There's a password
                masked_info = f"{auth_part[0]}:***@{connection_info[1]}"
                connection_info = masked_info
            else:
                connection_info = app.config["SQLALCHEMY_DATABASE_URI"]
        
        return jsonify({
            "database_version": version,
            "connection_info": connection_info,
            "table_counts": {
                "users": user_count,
                "faculty": faculty_count,
                "students": student_count,
                "courses": course_count,
                "attendance_records": attendance_count
            }
        })
    except Exception as e:
        logger.error(f"Error getting database info: {str(e)}")
        return jsonify({
            "status": "error",
            "message": f"Database error: {str(e)}"
        }), 500
@app.route('/edit_student/<int:id>', methods=['GET', 'POST'])
@admin_required
def edit_student(id):
    try:
        student = Student.query.get_or_404(id)
        
        if request.method == 'POST':
            # Update student information
            student.name = request.form.get('name')
            student.email = (request.form.get('email') or '').strip() or None
            student.phone = (request.form.get('phone') or '').strip() or None

            portal_password = (request.form.get('portal_password') or '').strip()
            if portal_password:
                student.password_hash = generate_password_hash(portal_password)
            elif not student.password_hash:
                # Ensure portal can log in — default to student_id
                student.password_hash = generate_password_hash(student.student_id)
            
            # Get selected courses
            course_ids = request.form.getlist('course_ids')
            
            # Update courses
            if course_ids:
                courses = Course.query.filter(Course.id.in_(course_ids)).all()
                student.courses = courses
            else:
                student.courses = []
            
            db.session.commit()
            flash('Student updated successfully!', 'success')
            return redirect(url_for('students'))
        
        # Get all courses for the form
        courses = Course.query.all()
        
        # Get the IDs of courses the student is already enrolled in
        enrolled_course_ids = [course.id for course in student.courses]
        
        return render_template('edit_student.html', student=student, courses=courses, enrolled_course_ids=enrolled_course_ids)
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error editing student: {str(e)}")
        flash(f"Error updating student: {str(e)}", "danger")
        return redirect(url_for('students'))

# ---------------------------------------------------------------------------
# Faculty Management Module
# ---------------------------------------------------------------------------

@app.route('/faculty_login', methods=['GET', 'POST'])
def faculty_login():
    error = None
    if is_faculty():
        return redirect(url_for('faculty_dashboard'))
    if is_admin():
        return redirect(url_for('dashboard'))
    if is_student():
        return redirect(url_for('student_dashboard'))

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')

        faculty = Faculty.query.filter_by(email=email).first()
        if not faculty or not check_password_hash(faculty.password_hash, password):
            error = 'Invalid email or password. Please try again.'
        else:
            clear_session_auth()
            session['faculty_db_id'] = faculty.id
            session['faculty_name'] = faculty.faculty_name
            session['faculty_id_code'] = faculty.faculty_id
            session['role'] = 'faculty'
            flash(f'Welcome, {faculty.faculty_name}!', 'success')
            return redirect(url_for('faculty_dashboard'))

    return render_template('faculty_login.html', error=error)


@app.route('/faculty_dashboard')
@faculty_required
def faculty_dashboard():
    faculty = get_current_faculty()
    if not faculty:
        clear_session_auth()
        return redirect(url_for('faculty_login'))

    assigned_courses = sorted(faculty.courses, key=lambda c: c.name or '')
    course_ids = [c.id for c in assigned_courses]

    today = datetime.now().date()
    today_attendance = 0
    student_count = 0
    if course_ids:
        today_attendance = Attendance.query.filter(
            Attendance.course_id.in_(course_ids),
            db.func.date(Attendance.timestamp) == today
        ).count()
        student_count = Student.query.join(student_course_association).filter(
            student_course_association.c.course_id.in_(course_ids)
        ).distinct().count()

    return render_template(
        'faculty_dashboard.html',
        faculty=faculty,
        assigned_courses=assigned_courses,
        course_count=len(assigned_courses),
        student_count=student_count,
        today_attendance=today_attendance
    )


@app.route('/faculty', methods=['GET', 'POST'])
@admin_required
def faculty_list():
    error = None
    if request.method == 'POST':
        faculty_id = request.form.get('faculty_id', '').strip()
        faculty_name = request.form.get('faculty_name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        department = request.form.get('department', '').strip()
        designation = request.form.get('designation', '').strip()
        password = request.form.get('password', '')
        course_ids = request.form.getlist('course_ids')

        try:
            if not all([faculty_id, faculty_name, email, department, designation, password]):
                flash('Please fill in all required fields.', 'danger')
            elif Faculty.query.filter_by(faculty_id=faculty_id).first():
                flash('Faculty ID already exists!', 'danger')
            elif Faculty.query.filter_by(email=email).first():
                flash('Email already registered for another faculty member!', 'danger')
            elif len(password) < 6:
                flash('Password must be at least 6 characters long.', 'danger')
            else:
                new_faculty = Faculty(
                    faculty_id=faculty_id,
                    faculty_name=faculty_name,
                    email=email,
                    phone=phone or None,
                    department=department,
                    designation=designation,
                    password_hash=generate_password_hash(password)
                )
                db.session.add(new_faculty)
                if course_ids:
                    assigned = Course.query.filter(Course.id.in_(course_ids)).all()
                    new_faculty.courses = assigned

                db.session.commit()
                flash('Faculty member added successfully!', 'success')
                return redirect(url_for('faculty_list'))
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error adding faculty: {str(e)}")
            flash(f"Database error: {str(e)}", "danger")

    try:
        faculty_members = Faculty.query.order_by(Faculty.faculty_name).all()
        courses = Course.query.order_by(Course.name).all()
    except Exception as e:
        logger.error(f"Error retrieving faculty list: {str(e)}")
        faculty_members = []
        courses = []
        flash(f"Database error: {str(e)}", "danger")

    return render_template(
        'faculty.html',
        faculty_members=faculty_members,
        courses=courses,
        error=error
    )


@app.route('/edit_faculty/<int:id>', methods=['GET', 'POST'])
@admin_required
def edit_faculty(id):
    faculty = Faculty.query.get_or_404(id)

    if request.method == 'POST':
        try:
            faculty_name = request.form.get('faculty_name', '').strip()
            email = request.form.get('email', '').strip()
            phone = request.form.get('phone', '').strip()
            department = request.form.get('department', '').strip()
            designation = request.form.get('designation', '').strip()
            password = request.form.get('password', '')
            course_ids = request.form.getlist('course_ids')

            email_owner = Faculty.query.filter(
                Faculty.email == email,
                Faculty.id != faculty.id
            ).first()
            if email_owner:
                flash('Email already used by another faculty member!', 'danger')
                return redirect(url_for('edit_faculty', id=id))

            faculty.faculty_name = faculty_name
            faculty.email = email
            faculty.phone = phone or None
            faculty.department = department
            faculty.designation = designation

            if password:
                if len(password) < 6:
                    flash('Password must be at least 6 characters long.', 'danger')
                    return redirect(url_for('edit_faculty', id=id))
                faculty.password_hash = generate_password_hash(password)

            if course_ids:
                assigned = Course.query.filter(Course.id.in_(course_ids)).all()
                faculty.courses = assigned
            else:
                faculty.courses = []

            db.session.commit()
            flash('Faculty updated successfully!', 'success')
            return redirect(url_for('faculty_list'))
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error editing faculty: {str(e)}")
            flash(f"Error updating faculty: {str(e)}", "danger")
            return redirect(url_for('edit_faculty', id=id))

    courses = Course.query.order_by(Course.name).all()
    assigned_course_ids = [c.id for c in faculty.courses]
    return render_template(
        'edit_faculty.html',
        faculty=faculty,
        courses=courses,
        assigned_course_ids=assigned_course_ids
    )


@app.route('/delete_faculty/<int:id>', methods=['POST'])
@admin_required
def delete_faculty(id):
    try:
        faculty = Faculty.query.get_or_404(id)
        db.session.delete(faculty)
        db.session.commit()
        flash('Faculty member deleted successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting faculty: {str(e)}")
        flash(f"Database error: {str(e)}", "danger")

    return redirect(url_for('faculty_list'))


@app.route('/assign_courses/<int:id>', methods=['GET', 'POST'])
@admin_required
def assign_courses(id):
    faculty = Faculty.query.get_or_404(id)

    if request.method == 'POST':
        try:
            course_ids = request.form.getlist('course_ids')
            if course_ids:
                assigned = Course.query.filter(Course.id.in_(course_ids)).all()
                faculty.courses = assigned
            else:
                faculty.courses = []
            db.session.commit()
            flash(f'Courses assigned to {faculty.faculty_name} successfully!', 'success')
            return redirect(url_for('faculty_list'))
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error assigning courses: {str(e)}")
            flash(f"Database error: {str(e)}", "danger")

    courses = Course.query.order_by(Course.name).all()
    assigned_course_ids = [c.id for c in faculty.courses]
    return render_template(
        'assign_courses.html',
        faculty=faculty,
        courses=courses,
        assigned_course_ids=assigned_course_ids
    )


# ---------------------------------------------------------------------------
# Timetable Module
# ---------------------------------------------------------------------------

WEEKDAY_NAMES = (
    'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'
)


def _parse_hhmm(value):
    """Parse 'HH:MM' or 'HH:MM:SS' into datetime.time."""
    if not value:
        return None
    value = value.strip()
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    return None


def get_current_timetable_slot(now=None, faculty_id=None):
    """
    Active timetable entry matching current weekday and clock time.
    For faculty, restrict to their slots; admin may get any matching slot.
    """
    now = now or datetime.now()
    day = now.weekday()  # Monday=0
    current = now.time()

    q = TimetableEntry.query.filter_by(day_of_week=day, is_active=True)
    if faculty_id:
        q = q.filter_by(faculty_id=faculty_id)
    elif is_faculty():
        q = q.filter_by(faculty_id=session.get('faculty_db_id'))

    candidates = q.all()
    matches = [
        e for e in candidates
        if e.start_time <= current <= e.end_time
    ]
    if not matches:
        return None
    # Prefer the slot that started most recently
    matches.sort(key=lambda e: e.start_time, reverse=True)
    return matches[0]


def _timetable_entries_query(department_id=None, semester_id=None, faculty_id=None,
                             course_ids=None):
    q = TimetableEntry.query.filter_by(is_active=True)
    if department_id:
        q = q.filter_by(department_id=int(department_id))
    if semester_id:
        q = q.filter_by(semester_id=int(semester_id))
    if faculty_id:
        q = q.filter_by(faculty_id=int(faculty_id))
    if course_ids is not None:
        q = q.join(Subject).filter(Subject.course_id.in_(course_ids or [-1]))
    return q.order_by(
        TimetableEntry.day_of_week,
        TimetableEntry.start_time
    )


def _entry_to_dict(e):
    return {
        'id': e.id,
        'day': e.day_of_week,
        'day_name': WEEKDAY_NAMES[e.day_of_week] if 0 <= e.day_of_week <= 6 else str(e.day_of_week),
        'start': e.start_time.strftime('%H:%M') if e.start_time else '',
        'end': e.end_time.strftime('%H:%M') if e.end_time else '',
        'room': e.room,
        'subject': e.subject.name if e.subject else '',
        'subject_code': e.subject.code if e.subject else '',
        'course_id': e.subject.course_id if e.subject else None,
        'course_name': e.subject.course.name if e.subject and e.subject.course else '',
        'faculty': e.faculty.faculty_name if e.faculty else '',
        'department': e.department.name if e.department else '',
        'semester': e.semester.name if e.semester else '',
        'is_now': False,
    }


def _mark_current_in_dicts(rows):
    now = datetime.now()
    day = now.weekday()
    cur = now.time()
    for r in rows:
        if r['day'] == day:
            try:
                st = _parse_hhmm(r['start'])
                et = _parse_hhmm(r['end'])
                if st and et and st <= cur <= et:
                    r['is_now'] = True
            except Exception:
                pass
    return rows


@app.route('/timetable', methods=['GET', 'POST'])
def timetable_page():
    """
    Admin: create departments/semesters/subjects and assign timetable slots.
    Faculty / Student: weekly timetable view.
    """
    if is_admin():
        if request.method == 'POST':
            action = request.form.get('action', '')
            try:
                if action == 'add_department':
                    name = (request.form.get('dept_name') or '').strip()
                    code = (request.form.get('dept_code') or '').strip() or None
                    if not name:
                        flash('Department name is required.', 'danger')
                    elif Department.query.filter_by(name=name).first() or (
                        code and Department.query.filter_by(code=code).first()
                    ):
                        flash('Department already exists.', 'warning')
                    else:
                        db.session.add(Department(name=name, code=code))
                        db.session.commit()
                        flash(f'Department "{name}" created.', 'success')

                elif action == 'add_semester':
                    name = (request.form.get('sem_name') or '').strip()
                    code = (request.form.get('sem_code') or '').strip() or None
                    year = (request.form.get('academic_year') or '').strip() or None
                    dept_id = request.form.get('sem_department_id') or None
                    if not name:
                        flash('Semester name is required.', 'danger')
                    else:
                        db.session.add(Semester(
                            name=name,
                            code=code,
                            academic_year=year,
                            department_id=int(dept_id) if dept_id else None,
                            is_active=True,
                        ))
                        db.session.commit()
                        flash(f'Semester "{name}" created.', 'success')

                elif action == 'add_subject':
                    name = (request.form.get('subject_name') or '').strip()
                    code = (request.form.get('subject_code') or '').strip()
                    dept_id = request.form.get('subject_department_id')
                    sem_id = request.form.get('subject_semester_id')
                    if not all([name, code, dept_id, sem_id]):
                        flash('Subject name, code, department, and semester are required.', 'danger')
                    else:
                        # Auto-find or create the matching Course (subject and course are unified)
                        course = Course.query.filter(
                            db.or_(Course.course_id == code, db.func.lower(Course.name) == name.lower())
                        ).first()
                        if not course:
                            course = Course(name=name, course_id=code)
                            db.session.add(course)
                            db.session.flush()

                        existing_sub = Subject.query.filter_by(code=code, semester_id=int(sem_id)).first()
                        if existing_sub:
                            existing_sub.course_id = course.id
                            db.session.commit()
                            flash(f'Subject "{name}" already exists and has been linked to course.', 'info')
                        else:
                            db.session.add(Subject(
                                name=name,
                                code=code,
                                department_id=int(dept_id),
                                semester_id=int(sem_id),
                                course_id=course.id,
                            ))
                            db.session.commit()
                            flash(f'Subject / Course "{name}" ({code}) created successfully.', 'success')

                elif action == 'assign_slot':
                    dept_id = request.form.get('slot_department_id')
                    sem_id = request.form.get('slot_semester_id')
                    subject_id = request.form.get('slot_subject_id')
                    faculty_id = request.form.get('slot_faculty_id')
                    day = request.form.get('slot_day')
                    start = _parse_hhmm(request.form.get('slot_start'))
                    end = _parse_hhmm(request.form.get('slot_end'))
                    room = (request.form.get('slot_room') or '').strip()
                    if not all([dept_id, sem_id, subject_id, faculty_id, day is not None, start, end, room]):
                        flash('All timetable fields are required.', 'danger')
                    elif end <= start:
                        flash('End time must be after start time.', 'danger')
                    else:
                        entry = TimetableEntry(
                            department_id=int(dept_id),
                            semester_id=int(sem_id),
                            subject_id=int(subject_id),
                            faculty_id=int(faculty_id),
                            day_of_week=int(day),
                            start_time=start,
                            end_time=end,
                            room=room,
                            is_active=True,
                        )
                        db.session.add(entry)
                        # Automatically assign faculty to this subject/course for attendance!
                        subject = Subject.query.get(int(subject_id))
                        faculty = Faculty.query.get(int(faculty_id))
                        if subject and faculty:
                            if not subject.course_id:
                                course = Course.query.filter(
                                    db.or_(Course.course_id == subject.code, db.func.lower(Course.name) == subject.name.lower())
                                ).first()
                                if not course:
                                    course = Course(name=subject.name, course_id=subject.code)
                                    db.session.add(course)
                                    db.session.flush()
                                subject.course_id = course.id
                            else:
                                course = Course.query.get(subject.course_id)
                            if course and course not in faculty.courses:
                                faculty.courses.append(course)
                        db.session.commit()
                        flash(f'Timetable slot assigned and {faculty.faculty_name} auto-linked to {subject.name}.', 'success')

                elif action == 'delete_slot':
                    slot_id = request.form.get('slot_id')
                    entry = TimetableEntry.query.get(int(slot_id))
                    if entry:
                        db.session.delete(entry)
                        db.session.commit()
                        flash('Timetable slot removed.', 'info')

                elif action == 'delete_department':
                    d = Department.query.get(int(request.form.get('id')))
                    if d:
                        db.session.delete(d)
                        db.session.commit()
                        flash('Department deleted.', 'info')

                elif action == 'delete_semester':
                    s = Semester.query.get(int(request.form.get('id')))
                    if s:
                        db.session.delete(s)
                        db.session.commit()
                        flash('Semester deleted.', 'info')

                elif action == 'delete_subject':
                    s = Subject.query.get(int(request.form.get('id')))
                    if s:
                        db.session.delete(s)
                        db.session.commit()
                        flash('Subject deleted.', 'info')

                elif action == 'toggle_semester':
                    s = Semester.query.get(int(request.form.get('id')))
                    if s:
                        s.is_active = not s.is_active
                        db.session.commit()
                        flash(f'Semester marked {"active" if s.is_active else "inactive"}.', 'success')

            except Exception as e:
                db.session.rollback()
                logger.error(f"Timetable admin action failed: {e}")
                flash(f'Error: {e}', 'danger')

            return redirect(url_for('timetable_page', tab=request.form.get('tab', 'assign')))

        departments = Department.query.order_by(Department.name).all()
        semesters = Semester.query.order_by(Semester.name).all()
        subjects = Subject.query.order_by(Subject.name).all()
        faculty_list = Faculty.query.order_by(Faculty.faculty_name).all()
        courses = Course.query.order_by(Course.name).all()
        entries = TimetableEntry.query.order_by(
            TimetableEntry.day_of_week, TimetableEntry.start_time
        ).all()
        entry_rows = _mark_current_in_dicts([_entry_to_dict(e) for e in entries])
        current_slot = get_current_timetable_slot()

        return render_template(
            'timetable_admin.html',
            departments=departments,
            semesters=semesters,
            subjects=subjects,
            faculty_list=faculty_list,
            courses=courses,
            entries=entries,
            entry_rows=entry_rows,
            weekdays=WEEKDAY_NAMES,
            current_slot=current_slot,
            active_tab=request.args.get('tab', 'assign'),
        )

    if is_faculty() or is_student():
        filter_sem = request.args.get('semester_id') or None
        filter_dept = request.args.get('department_id') or None

        if is_faculty():
            q = _timetable_entries_query(
                department_id=filter_dept,
                semester_id=filter_sem,
                faculty_id=session.get('faculty_db_id'),
            )
            role = 'faculty'
        else:
            student = get_current_student()
            course_ids = [c.id for c in student.courses] if student else []
            q = _timetable_entries_query(
                department_id=filter_dept,
                semester_id=filter_sem,
                course_ids=course_ids,
            )
            role = 'student'

        entries = q.all()
        entry_rows = _mark_current_in_dicts([_entry_to_dict(e) for e in entries])
        current_slot = None
        if is_faculty():
            current_slot = get_current_timetable_slot(faculty_id=session.get('faculty_db_id'))
        else:
            # Student: any current slot among their courses
            now = datetime.now()
            for e in entries:
                if e.day_of_week == now.weekday() and e.start_time <= now.time() <= e.end_time:
                    current_slot = e
                    break

        return render_template(
            'timetable_view.html',
            entry_rows=entry_rows,
            weekdays=WEEKDAY_NAMES,
            current_slot=current_slot,
            departments=Department.query.order_by(Department.name).all(),
            semesters=Semester.query.filter_by(is_active=True).order_by(Semester.name).all(),
            filter_sem=filter_sem,
            filter_dept=filter_dept,
            role=role,
        )

    flash('Please log in to view the timetable.', 'warning')
    return redirect(url_for('login'))


@app.route('/api/timetable/current')
def api_timetable_current():
    """JSON: current live slot for attendance auto-select."""
    if not is_admin() and not is_faculty():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    slot = get_current_timetable_slot(
        faculty_id=session.get('faculty_db_id') if is_faculty() else None
    )
    if not slot:
        return jsonify({"status": "success", "slot": None})
    data = _entry_to_dict(slot)
    data['is_now'] = True
    return jsonify({"status": "success", "slot": data})


# ---------------------------------------------------------------------------
# Student Portal Module
# ---------------------------------------------------------------------------

def _student_course_stats(student):
    """Attendance % per enrolled course for one student."""
    course_ids = [c.id for c in student.courses]
    if not course_ids:
        return []
    records = build_defaulter_records(course_ids=course_ids, status_filter=None)
    return [r for r in records if r['student_id'] == student.id]


def _student_overall_percentage(course_stats):
    scored = [r['attendance_percentage'] for r in course_stats if r['attendance_percentage'] is not None]
    if not scored:
        return None
    return round(sum(scored) / len(scored), 2)


@app.route('/student_login', methods=['GET', 'POST'])
def student_login():
    error = None
    if is_student():
        return redirect(url_for('student_dashboard'))
    if is_admin():
        return redirect(url_for('dashboard'))
    if is_faculty():
        return redirect(url_for('faculty_dashboard'))

    if request.method == 'POST':
        login_id = (request.form.get('login_id') or '').strip()
        password = request.form.get('password') or ''

        student = Student.query.filter(
            db.or_(
                Student.student_id == login_id,
                Student.email == login_id
            )
        ).first()

        if not student or not student.password_hash:
            error = 'Invalid credentials or portal access not enabled. Contact admin.'
        elif not check_password_hash(student.password_hash, password):
            error = 'Invalid Student ID/email or password.'
        else:
            clear_session_auth()
            session['student_db_id'] = student.id
            session['student_name'] = student.name
            session['student_code'] = student.student_id
            session['role'] = 'student'
            flash(f'Welcome, {student.name}!', 'success')
            return redirect(url_for('student_dashboard'))

    return render_template('student_login.html', error=error)


@app.route('/student_dashboard')
@student_required
def student_dashboard():
    student = get_current_student()
    if not student:
        clear_session_auth()
        return redirect(url_for('student_login'))

    courses = list(student.courses)
    course_stats = _student_course_stats(student)
    overall = _student_overall_percentage(course_stats)
    today = datetime.now().date()
    today_count = Attendance.query.filter(
        Attendance.student_id == student.id,
        db.func.date(Attendance.timestamp) == today
    ).count()
    history_count = Attendance.query.filter_by(student_id=student.id).count()
    defaulter_count = sum(1 for r in course_stats if r['status'] == 'Defaulter')

    recent = Attendance.query.filter_by(student_id=student.id).order_by(
        Attendance.timestamp.desc()
    ).limit(8).all()

    return render_template(
        'student_dashboard.html',
        student=student,
        courses=courses,
        course_stats=course_stats,
        overall_percentage=overall,
        today_count=today_count,
        history_count=history_count,
        defaulter_count=defaulter_count,
        recent_attendance=recent,
        threshold=DEFAULTER_THRESHOLD
    )


@app.route('/student_profile', methods=['GET', 'POST'])
@student_required
def student_profile():
    student = get_current_student()
    if not student:
        return redirect(url_for('student_login'))

    if request.method == 'POST':
        action = request.form.get('action') or 'profile'
        try:
            if action == 'profile':
                student.name = (request.form.get('name') or student.name).strip()
                student.email = (request.form.get('email') or '').strip() or None
                student.phone = (request.form.get('phone') or '').strip() or None
                db.session.commit()
                session['student_name'] = student.name
                flash('Profile updated successfully.', 'success')
            elif action == 'password':
                current_password = request.form.get('current_password') or ''
                new_password = request.form.get('new_password') or ''
                confirm_password = request.form.get('confirm_password') or ''
                if not student.password_hash or not check_password_hash(student.password_hash, current_password):
                    flash('Current password is incorrect.', 'danger')
                elif len(new_password) < 6:
                    flash('New password must be at least 6 characters.', 'danger')
                elif new_password != confirm_password:
                    flash('New password and confirmation do not match.', 'danger')
                else:
                    student.password_hash = generate_password_hash(new_password)
                    db.session.commit()
                    flash('Password changed successfully.', 'success')
            return redirect(url_for('student_profile'))
        except Exception as e:
            db.session.rollback()
            logger.error(f"Student profile update error: {e}")
            flash(f'Could not update profile: {e}', 'danger')

    return render_template('student_profile.html', student=student)


@app.route('/student_courses')
@student_required
def student_courses():
    student = get_current_student()
    if not student:
        return redirect(url_for('student_login'))
    course_stats = {r['course_id']: r for r in _student_course_stats(student)}
    return render_template(
        'student_courses.html',
        student=student,
        courses=list(student.courses),
        course_stats=course_stats,
        threshold=DEFAULTER_THRESHOLD
    )


@app.route('/student_attendance')
@student_required
def student_attendance():
    student = get_current_student()
    if not student:
        return redirect(url_for('student_login'))

    course_stats = _student_course_stats(student)
    overall = _student_overall_percentage(course_stats)

    course_id = request.args.get('course_id', 'all')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    query = Attendance.query.filter_by(student_id=student.id)
    enrolled_ids = [c.id for c in student.courses]
    if course_id and course_id != 'all':
        try:
            cid = int(course_id)
            if cid not in enrolled_ids:
                flash('You are not enrolled in that course.', 'warning')
                return redirect(url_for('student_attendance'))
            query = query.filter(Attendance.course_id == cid)
        except ValueError:
            pass
    if date_from:
        query = query.filter(Attendance.timestamp >= date_from)
    if date_to:
        query = query.filter(Attendance.timestamp <= date_to + " 23:59:59")

    history = query.order_by(Attendance.timestamp.desc()).limit(500).all()

    return render_template(
        'student_attendance.html',
        student=student,
        course_stats=course_stats,
        overall_percentage=overall,
        history=history,
        courses=list(student.courses),
        selected_course=course_id,
        date_from=date_from or '',
        date_to=date_to or '',
        threshold=DEFAULTER_THRESHOLD
    )


@app.route('/student_reports')
@student_required
def student_reports():
    student = get_current_student()
    if not student:
        return redirect(url_for('student_login'))
    return render_template(
        'student_reports.html',
        student=student,
        courses=list(student.courses)
    )


@app.route('/student_reports/export/<fmt>')
@student_required
def student_reports_export(fmt):
    """Student downloads their own attendance report (PDF / Excel / CSV)."""
    student = get_current_student()
    if not student:
        return redirect(url_for('student_login'))

    fmt = (fmt or '').lower().strip()
    if fmt not in ('pdf', 'excel', 'csv', 'xlsx'):
        flash('Unsupported export format.', 'danger')
        return redirect(url_for('student_reports'))
    if fmt == 'xlsx':
        fmt = 'excel'

    # Force filter to current student only
    args = {
        'student_id': str(student.id),
        'course_id': request.args.get('course_id', 'all'),
        'date_from': request.args.get('date_from', ''),
        'date_to': request.args.get('date_to', ''),
    }
    # Temporarily elevate query path: reuse shared helper but must skip faculty/admin gates.
    # query_attendance_report_records checks is_faculty/admin for auth — student isn't either.
    # So build the query inline for the student.
    try:
        query = db.session.query(
            Attendance,
            Student.name.label('student_name'),
            Student.student_id.label('student_code'),
            Course.id.label('course_pk'),
            Course.name.label('course_name'),
            Course.course_id.label('course_code'),
        ).select_from(Attendance).join(
            Student, Attendance.student_id == Student.id
        ).join(
            Course, Attendance.course_id == Course.id
        ).filter(Attendance.student_id == student.id)

        course_id = args.get('course_id')
        enrolled_ids = [c.id for c in student.courses]
        if course_id and course_id != 'all':
            cid = int(course_id)
            if cid not in enrolled_ids:
                flash('Unauthorized course.', 'danger')
                return redirect(url_for('student_reports'))
            query = query.filter(Attendance.course_id == cid)
        if args.get('date_from'):
            query = query.filter(Attendance.timestamp >= args['date_from'])
        if args.get('date_to'):
            query = query.filter(Attendance.timestamp <= args['date_to'] + " 23:59:59")

        results = query.order_by(Attendance.timestamp.desc()).all()
        faculty_map, dept_map = _faculty_dept_maps()
        records = []
        for record in results:
            attendance = record[0]
            cid = record.course_pk
            records.append({
                'id': attendance.id,
                'student_name': record.student_name,
                'student_code': record.student_code,
                'course_name': record.course_name,
                'course_code': record.course_code,
                'faculty_names': faculty_map.get(cid, ''),
                'departments': dept_map.get(cid, ''),
                'timestamp': attendance.timestamp.isoformat(),
                'date': attendance.timestamp.strftime('%Y-%m-%d'),
                'time': attendance.timestamp.strftime('%H:%M:%S'),
            })

        from report_service import (
            generate_attendance_pdf,
            generate_attendance_excel,
            generate_attendance_csv,
        )
        meta = {
            'date_from': args.get('date_from') or '',
            'date_to': args.get('date_to') or '',
            'course_label': 'All',
            'student_label': f"{student.name} ({student.student_id})",
            'faculty_label': '—',
            'department_label': '—',
        }
        if course_id and course_id != 'all':
            c = Course.query.get(int(course_id))
            if c:
                meta['course_label'] = f"{c.name} ({c.course_id})"

        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        if fmt == 'pdf':
            buffer = generate_attendance_pdf(records, meta)
            return send_file(buffer, as_attachment=True,
                             download_name=f'my_attendance_{stamp}.pdf',
                             mimetype='application/pdf')
        if fmt == 'excel':
            buffer = generate_attendance_excel(records, meta)
            return send_file(
                buffer, as_attachment=True,
                download_name=f'my_attendance_{stamp}.xlsx',
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
        buffer = generate_attendance_csv(records)
        return send_file(buffer, as_attachment=True,
                         download_name=f'my_attendance_{stamp}.csv',
                         mimetype='text/csv')
    except Exception as e:
        logger.error(f"Student report export error: {e}")
        flash(f'Could not export report: {e}', 'danger')
        return redirect(url_for('student_reports'))


@app.route('/db_admin')
@admin_required
def db_admin():
    """Database admin page"""
    try:
        # Get database version
        version_query = db.session.execute(db.text("SELECT version()")).fetchone()
        db_version = version_query[0] if version_query else "Unknown"
        
        # Get PostgreSQL connection info for displaying
        pg_host = os.environ.get("PGHOST", "localhost")
        pg_port = os.environ.get("PGPORT", "5432")
        pg_user = os.environ.get("PGUSER", "postgres")
        pg_database = os.environ.get("PGDATABASE", "postgres")
        
        # Get table counts
        tables = {
            "Users": User.query.count(),
            "Faculty": Faculty.query.count(),
            "Students": Student.query.count(),
            "Courses": Course.query.count(),
            "Attendance Records": Attendance.query.count()
        }
        
        # Get connection info to display in template
        db_info = {
            'type': 'PostgreSQL',
            'host': pg_host,
            'port': pg_port,
            'user': pg_user,
            'database': pg_database,
            'tables': {
                'users': tables["Users"],
                'faculty': tables["Faculty"],
                'students': tables["Students"], 
                'courses': tables["Courses"],
                'attendances': tables["Attendance Records"]
            }
        }
        
        # Schema information
        schema_query = db.session.execute(db.text("""
            SELECT table_name, column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
        """)).fetchall()
        
        # Organize by table
        schema = {}
        for row in schema_query:
            table = row[0]
            if table not in schema:
                schema[table] = []
            schema[table].append({
                "column": row[1],
                "type": row[2],
                "nullable": row[3]
            })
        
        # Index information
        index_query = db.session.execute(db.text("""
            SELECT
                t.relname AS table_name,
                i.relname AS index_name,
                array_to_string(array_agg(a.attname), ', ') AS column_names
            FROM
                pg_class t,
                pg_class i,
                pg_index ix,
                pg_attribute a
            WHERE
                t.oid = ix.indrelid
                AND i.oid = ix.indexrelid
                AND a.attrelid = t.oid
                AND a.attnum = ANY(ix.indkey)
                AND t.relkind = 'r'
                AND t.relname NOT LIKE 'pg_%'
            GROUP BY
                t.relname,
                i.relname
            ORDER BY
                t.relname,
                i.relname;
        """)).fetchall()
        
        indices = {}
        for row in index_query:
            table = row[0]
            if table not in indices:
                indices[table] = []
            indices[table].append({
                "name": row[1],
                "columns": row[2]
            })
        
        # Get connection information
        connection_info = app.config.get("SQLALCHEMY_DATABASE_URI", "")
        # Mask password for security
        if '@' in connection_info:
            connection_info = connection_info.split('@')
            auth_part = connection_info[0].split(':')
            if len(auth_part) > 2:  # There's a password
                masked_info = f"{auth_part[0]}:***@{connection_info[1]}"
                connection_info = masked_info
            
        # Check database connection and constraints
        db_status = "Connected"
        constraints_status = "Valid"
        
        try:
            # Test a quick query
            db.session.execute(db.text("SELECT 1")).fetchone()
            
            # Additional check for foreign key constraints
            # This query checks for FK violations
            fk_check = db.session.execute(db.text("""
                SELECT c.conname AS constraint_name,
                       c.conrelid::regclass AS table_name
                FROM pg_constraint c
                JOIN pg_namespace n ON n.oid = c.connamespace
                WHERE c.contype = 'f'
                  AND n.nspname = 'public'
                ORDER BY c.conname;
            """)).fetchall()
            
            if not fk_check:
                constraints_status = "Unknown (no foreign keys found)"
                
        except Exception as e:
            db_status = f"Error: {str(e)}"
            constraints_status = "Unknown (couldn't verify)"
            
        # Sample data for pgAdmin instructions
        pgadmin_instructions = [
            f"1. Install pgAdmin from https://www.pgadmin.org/",
            f"2. Launch pgAdmin and click 'Add New Server'",
            f"3. On the General tab, give your connection a name (e.g., 'Attendance System')",
            f"4. On the Connection tab, enter these details:",
            f"   - Host: {pg_host}",
            f"   - Port: {pg_port}",
            f"   - Maintenance Database: {pg_database}",
            f"   - Username: {pg_user}",
            f"5. Click Save to connect"
        ]
        
        # Troubleshooting info
        troubleshooting = {
            "common_errors": [
                {"error": "FATAL: password authentication failed for user", 
                 "solution": "Check that the correct database password is set in the PGPASSWORD environment variable"},
                {"error": "FATAL: role 'username' does not exist", 
                 "solution": "Ensure the user specified in PGUSER exists in PostgreSQL"},
                {"error": "FATAL: database 'dbname' does not exist", 
                 "solution": "Create the database specified in PGDATABASE or update the variable"},
                {"error": "could not connect to server: Connection refused", 
                 "solution": "Verify PostgreSQL is running and accessible at the host/port specified"},
                {"error": "no password supplied", 
                 "solution": "Set the PGPASSWORD environment variable"}
            ],
            "connection_checklist": [
                "PostgreSQL server is running",
                "Database credentials are correct",
                "Network allows connections to the database port",
                "Database exists",
                "User has proper permissions"
            ]
        }
        
        return render_template('db_admin.html', 
                             db_info=db_info, 
                             version=db_version,
                             tables=tables,
                             schema=schema,
                             indices=indices,
                             connection=connection_info,
                             db_status=db_status,
                             constraints=constraints_status,
                             troubleshooting=troubleshooting,
                             pgadmin_instructions=pgadmin_instructions)
    
    except Exception as e:
        logger.error(f"Error in db_admin: {str(e)}")
        flash(f"Database error: {str(e)}", "danger")
        return render_template('db_admin.html', error=str(e))
@app.route('/flow_diagram')
@admin_required
def flow_diagram():
    """System flow diagram page"""
    return render_template('flow_diagram.html')

@app.route('/er_diagram')
@admin_required
def er_diagram():
    """Entity relationship diagram page"""
    return render_template('er_diagram.html')

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    """Handle forgot password requests using security questions - First step"""
    message = None
    error = None
    security_question = None
    
    if request.method == 'POST':
        email = request.form.get('email')
        email_submitted = request.form.get('email_submitted')
        
        if not email:
            error = 'Email is required'
        else:
            # Find user by email
            user = User.query.filter_by(email=email).first()
            
            if not user:
                error = 'No account found with that email address'
            elif not email_submitted:
                # First step - show security question
                if not user.security_question:
                    error = 'This account does not have a security question set up. Please contact support.'
                else:
                    # Show the security question in the form
                    security_question = user.security_question
            else:
                # Second step - verify security answer
                security_answer = request.form.get('security_answer')
                
                if not security_answer:
                    error = 'Security answer is required'
                    security_question = user.security_question
                elif not user.verify_security_answer(security_answer):
                    error = 'Incorrect security answer'
                    security_question = user.security_question
                else:
                    # Store the user's email in the session for the reset page
                    session['reset_email'] = user.email
                    # Set a timestamp to expire the reset session after 15 minutes
                    session['reset_expiry'] = datetime.utcnow().timestamp() + 900  # 15 minutes
                    # Redirect to the reset password page
                    return redirect(url_for('reset_password_direct'))
    
    return render_template('forgot_password.html', message=message, error=error, security_question=security_question)

@app.route('/reset_password_direct', methods=['GET', 'POST'])
def reset_password_direct():
    """Handle direct password reset after security question verification"""
    # Check if reset session is valid
    if 'reset_email' not in session or 'reset_expiry' not in session:
        flash('Password reset session not found. Please try again.', 'danger')
        return redirect(url_for('forgot_password'))
    
    # Check if reset session is expired
    if datetime.utcnow().timestamp() > session.get('reset_expiry', 0):
        # Clear expired session data
        session.pop('reset_email', None)
        session.pop('reset_expiry', None)
        flash('The password reset session has expired. Please try again.', 'warning')
        return redirect(url_for('forgot_password'))
    
    error = None
    if request.method == 'POST':
        new_password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if not new_password or len(new_password) < 8:
            error = 'New password must be at least 8 characters long'
        elif new_password != confirm_password:
            error = 'Passwords do not match'
        else:
            try:
                # Retrieve user by email stored in session
                user = User.query.filter_by(email=session['reset_email']).first()
                if user:
                    # Update the user's password
                    user.password_hash = generate_password_hash(new_password)
                    db.session.commit()
                    
                    # Clear reset session data
                    session.pop('reset_email', None)
                    session.pop('reset_expiry', None)
                    
                    flash('Your password has been reset successfully! You can now login.', 'success')
                    return redirect(url_for('login'))
                else:
                    error = 'User not found. Please try the reset process again.'
            except Exception as e:
                db.session.rollback()
                logger.error(f"Error resetting password: {str(e)}")
                error = f"Database error: {str(e)}"
    
    return render_template('reset_password.html', error=error)

@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    """Legacy route for token-based password reset - redirects to security question based reset"""
    flash('We have updated our password reset system. Please use the security question method below.', 'info')
    return redirect(url_for('forgot_password'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)