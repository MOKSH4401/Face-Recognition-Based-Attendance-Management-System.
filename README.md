# AttendSys — PIN + GPS Attendance

Web attendance for classes using a **rotating 6-digit PIN**, **authenticated student login**, and **server-side classroom GPS**. Face recognition is not used.

GPS can be spoofed and cannot prove physical presence. PIN + login + location is a deterrent, not proof.

## Features

- Faculty starts a timed session for a course (and optional subject)
- Cryptographically generated PIN rotates every 30–60 seconds (default 45)
- Logged-in enrolled students enter the PIN, then share browser geolocation
- Backend checks identity, enrollment, active session, PIN, coordinates, radius, and GPS accuracy
- Unique `(session_id, student_id)` index blocks duplicate marks
- Faculty dashboard updates in real time over Socket.IO
- Existing student/faculty/admin dashboards, reports, timetable, and notifications are unchanged

## Technology stack

- Backend: Python, Flask, Flask-SocketIO
- Database: PostgreSQL, SQLAlchemy
- Frontend: HTML, CSS, JavaScript, Bootstrap, Socket.IO client

## Setup

```bash
cd Face-Recognition-Based-Attendance-Management-System
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set:

```
SESSION_SECRET=change-me
DATABASE_URL=postgresql://postgres:root@localhost:5432/attendance_db
CLASSROOM_RADIUS_METERS=80
GPS_ACCURACY_BUFFER_METERS=25
GPS_MAX_ACCURACY_METERS=80
SESSION_DEFAULT_MINUTES=15
PIN_PERIOD_SECONDS=45
```

Create the database, then:

```bash
python -c "from app import db, app; app.app_context().push(); db.create_all()"
python main.py
```

Open http://localhost:5000

## Attendance workflow

1. Faculty (or admin) opens **PIN Attendance**, selects class/subject, starts a session (classroom GPS is taken from the course or this device).
2. Share the live PIN with students in the room. It refreshes automatically.
3. Eligible students see **Mark Attendance**, enter the PIN, allow location.
4. Server marks **PRESENT** only if every check passes.

## Tests

```bash
python -m pytest tests -q
```

## Main APIs

| Method | Path | Who | Purpose |
| --- | --- | --- | --- |
| POST | `/api/sessions/start` | Faculty/admin | Start PIN session |
| POST | `/api/sessions/<id>/stop` | Faculty/admin | Close session |
| GET | `/api/sessions/<id>/pin` | Faculty/admin | Current PIN |
| GET | `/api/sessions/<id>/live` | Faculty/enrolled student | Live roster (PIN only for faculty) |
| GET | `/api/student/active_session` | Student | Active session (no PIN) |
| POST | `/api/student/validate_pin` | Student | Check PIN (does not mark) |
| POST | `/api/student/mark_attendance` | Student | PIN + GPS mark (server re-checks everything) |

Socket.IO: `join_session` (faculty), `join_student` (student). Events: `session_started`, `attendance_update`.
