from app import db
from datetime import datetime, timedelta
from flask_login import UserMixin
import secrets

# Many-to-many: students enrolled in courses
student_course_association = db.Table('student_course_association',
    db.Column('student_id', db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), primary_key=True),
    db.Column('course_id', db.Integer, db.ForeignKey('courses.id', ondelete='CASCADE'), primary_key=True)
)

# Many-to-many: faculty assigned to courses
faculty_course_association = db.Table('faculty_course_association',
    db.Column('faculty_id', db.Integer, db.ForeignKey('faculty.id', ondelete='CASCADE'), primary_key=True),
    db.Column('course_id', db.Integer, db.ForeignKey('courses.id', ondelete='CASCADE'), primary_key=True)
)


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    __table_args__ = {'extend_existing': True}

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    reset_token = db.Column(db.String(100), nullable=True)
    reset_token_expiry = db.Column(db.DateTime, nullable=True)
    security_question = db.Column(db.String(200), nullable=True)
    security_answer = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def generate_reset_token(self):
        self.reset_token = secrets.token_urlsafe(32)
        self.reset_token_expiry = datetime.utcnow() + timedelta(hours=24)
        return self.reset_token

    def verify_reset_token(self, token):
        if self.reset_token != token:
            return False
        if self.reset_token_expiry is None or self.reset_token_expiry < datetime.utcnow():
            return False
        return True

    def clear_reset_token(self):
        self.reset_token = None
        self.reset_token_expiry = None

    def verify_security_answer(self, answer):
        if not self.security_answer or not answer:
            return False
        return self.security_answer.lower() == answer.lower().strip()


class Faculty(db.Model):
    __tablename__ = 'faculty'

    id = db.Column(db.Integer, primary_key=True)
    faculty_id = db.Column(db.String(20), unique=True, nullable=False)
    faculty_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    department = db.Column(db.String(100), nullable=False)
    designation = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    courses = db.relationship(
        'Course',
        secondary=faculty_course_association,
        backref=db.backref('faculty_members', lazy='dynamic')
    )

    def __repr__(self):
        return f'<Faculty {self.faculty_id}: {self.faculty_name}>'


class Student(db.Model):
    __tablename__ = 'students'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    student_id = db.Column(db.String(20), unique=True, nullable=False)
    email = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    password_hash = db.Column(db.String(256), nullable=True)  # Student portal login
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    courses = db.relationship('Course', secondary=student_course_association,
                            backref=db.backref('students', lazy='dynamic'))
    attendances = db.relationship('Attendance', backref='student', lazy=True, cascade="all, delete-orphan")


class Course(db.Model):
    __tablename__ = 'courses'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    course_id = db.Column(db.String(20), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    classroom_lat = db.Column(db.Float, nullable=True)
    classroom_lng = db.Column(db.Float, nullable=True)
    classroom_radius_meters = db.Column(db.Float, nullable=True)

    attendances = db.relationship('Attendance', backref='course', lazy=True, cascade="all, delete-orphan")
    attendance_sessions = db.relationship(
        'AttendanceSession', backref='course', lazy=True, cascade='all, delete-orphan'
    )

    @property
    def subject(self):
        """Unified access to the corresponding Subject."""
        return self.subjects[0] if self.subjects else None


class AttendanceSession(db.Model):
    """Faculty-started window during which enrolled students may self-mark."""
    __tablename__ = 'attendance_sessions'

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('courses.id', ondelete='CASCADE'), nullable=False)
    faculty_id = db.Column(db.Integer, db.ForeignKey('faculty.id', ondelete='SET NULL'), nullable=True)
    started_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    ends_at = db.Column(db.DateTime, nullable=False)
    closed_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    classroom_lat = db.Column(db.Float, nullable=False)
    classroom_lng = db.Column(db.Float, nullable=False)
    radius_meters = db.Column(db.Float, nullable=False)
    pin_secret = db.Column(db.String(64), nullable=True)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id', ondelete='SET NULL'), nullable=True)
    note = db.Column(db.String(200), nullable=True)

    faculty = db.relationship('Faculty', backref=db.backref('attendance_sessions', lazy=True))
    subject = db.relationship('Subject', backref=db.backref('attendance_sessions', lazy=True))
    attendances = db.relationship('Attendance', backref='session', lazy=True)

    __table_args__ = (
        db.Index('idx_session_course_active', course_id, is_active),
    )


class Attendance(db.Model):
    __tablename__ = 'attendances'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('courses.id', ondelete='CASCADE'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    session_id = db.Column(db.Integer, db.ForeignKey('attendance_sessions.id', ondelete='SET NULL'), nullable=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    gps_accuracy = db.Column(db.Float, nullable=True)
    distance_meters = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), nullable=False, default='present')

    __table_args__ = (
        db.Index('idx_attendance_date', timestamp),
        db.Index('idx_student_course', student_id, course_id),
        db.Index('idx_attendance_session_student', session_id, student_id),
        db.UniqueConstraint('session_id', 'student_id', name='uq_attendance_session_student'),
    )


class EmailSettings(db.Model):
    """Admin-configurable SMTP settings (single active row expected)."""
    __tablename__ = 'email_settings'

    id = db.Column(db.Integer, primary_key=True)
    mail_server = db.Column(db.String(120), nullable=True, default='smtp.gmail.com')
    mail_port = db.Column(db.Integer, nullable=True, default=587)
    mail_use_tls = db.Column(db.Boolean, default=True, nullable=False)
    mail_use_ssl = db.Column(db.Boolean, default=False, nullable=False)
    mail_username = db.Column(db.String(120), nullable=True)
    mail_password = db.Column(db.String(256), nullable=True)
    mail_default_sender = db.Column(db.String(120), nullable=True)
    auto_notify_enabled = db.Column(db.Boolean, default=True, nullable=False)
    attendance_threshold = db.Column(db.Float, default=75.0, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EmailNotificationLog(db.Model):
    """History of defaulter warning emails (auto and manual)."""
    __tablename__ = 'email_notification_logs'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='SET NULL'), nullable=True)
    course_id = db.Column(db.Integer, db.ForeignKey('courses.id', ondelete='SET NULL'), nullable=True)
    recipient_email = db.Column(db.String(120), nullable=False)
    subject = db.Column(db.String(255), nullable=True)
    attendance_percentage = db.Column(db.Float, nullable=True)
    trigger = db.Column(db.String(20), nullable=False, default='manual')  # auto | manual | test
    status = db.Column(db.String(20), nullable=False, default='pending')  # pending | sent | failed
    error_message = db.Column(db.String(500), nullable=True)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('Student', backref=db.backref('email_logs', lazy=True))
    course = db.relationship('Course', backref=db.backref('email_logs', lazy=True))


class Notification(db.Model):
    """Admin-created in-app notification broadcast."""
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    priority = db.Column(db.String(20), nullable=False, default='info')  # info | warning | urgent
    audience = db.Column(db.String(20), nullable=False, default='all')  # all | faculty | students | course
    course_id = db.Column(db.Integer, db.ForeignKey('courses.id', ondelete='SET NULL'), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    course = db.relationship('Course', backref=db.backref('notifications', lazy=True))
    creator = db.relationship('User', backref=db.backref('created_notifications', lazy=True))
    deliveries = db.relationship(
        'NotificationDelivery',
        backref='notification',
        lazy=True,
        cascade='all, delete-orphan'
    )

    def __repr__(self):
        return f'<Notification {self.id}: {self.title}>'


class NotificationDelivery(db.Model):
    """Per-recipient delivery + read state for a notification."""
    __tablename__ = 'notification_deliveries'

    id = db.Column(db.Integer, primary_key=True)
    notification_id = db.Column(
        db.Integer, db.ForeignKey('notifications.id', ondelete='CASCADE'), nullable=False
    )
    recipient_role = db.Column(db.String(20), nullable=False)  # faculty | student
    faculty_id = db.Column(db.Integer, db.ForeignKey('faculty.id', ondelete='CASCADE'), nullable=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id', ondelete='CASCADE'), nullable=True)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    read_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    faculty = db.relationship('Faculty', backref=db.backref('notification_deliveries', lazy=True))
    student = db.relationship('Student', backref=db.backref('notification_deliveries', lazy=True))

    __table_args__ = (
        db.Index('idx_notif_delivery_faculty', faculty_id, is_read),
        db.Index('idx_notif_delivery_student', student_id, is_read),
    )


class Department(db.Model):
    """Academic department for timetable organization."""
    __tablename__ = 'departments'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    code = db.Column(db.String(20), unique=True, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    semesters = db.relationship('Semester', backref='department', lazy=True)
    subjects = db.relationship('Subject', backref='department', lazy=True)
    timetable_entries = db.relationship('TimetableEntry', backref='department', lazy=True)

    def __repr__(self):
        return f'<Department {self.name}>'


class Semester(db.Model):
    """Semester / term under a department (or shared)."""
    __tablename__ = 'semesters'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(20), nullable=True)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id', ondelete='SET NULL'), nullable=True)
    academic_year = db.Column(db.String(20), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    subjects = db.relationship('Subject', backref='semester', lazy=True)
    timetable_entries = db.relationship('TimetableEntry', backref='semester', lazy=True)

    def __repr__(self):
        return f'<Semester {self.name}>'


class Subject(db.Model):
    """
    Timetable subject linked to an attendance Course.
    PIN+GPS attendance uses course_id enrollments.
    """
    __tablename__ = 'subjects'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    code = db.Column(db.String(30), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id', ondelete='CASCADE'), nullable=False)
    semester_id = db.Column(db.Integer, db.ForeignKey('semesters.id', ondelete='CASCADE'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('courses.id', ondelete='SET NULL'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    course = db.relationship('Course', backref=db.backref('subjects', lazy=True))
    timetable_entries = db.relationship('TimetableEntry', backref='subject', lazy=True)

    def __repr__(self):
        return f'<Subject {self.code}: {self.name}>'


class TimetableEntry(db.Model):
    """One scheduled class slot: subject, faculty, time, room."""
    __tablename__ = 'timetable_entries'

    id = db.Column(db.Integer, primary_key=True)
    department_id = db.Column(db.Integer, db.ForeignKey('departments.id', ondelete='CASCADE'), nullable=False)
    semester_id = db.Column(db.Integer, db.ForeignKey('semesters.id', ondelete='CASCADE'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subjects.id', ondelete='CASCADE'), nullable=False)
    faculty_id = db.Column(db.Integer, db.ForeignKey('faculty.id', ondelete='CASCADE'), nullable=False)
    day_of_week = db.Column(db.Integer, nullable=False)  # 0=Monday … 6=Sunday
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    room = db.Column(db.String(50), nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    faculty = db.relationship('Faculty', backref=db.backref('timetable_entries', lazy=True))

    __table_args__ = (
        db.Index('idx_timetable_day_time', day_of_week, start_time, end_time),
        db.Index('idx_timetable_faculty_day', faculty_id, day_of_week),
    )

    def __repr__(self):
        return f'<TimetableEntry {self.id} day={self.day_of_week} {self.room}>'

