"""Faculty PIN sessions + student self-mark (authenticated login + GPS)."""
import logging
from datetime import datetime, timedelta
from threading import Lock

from flask import jsonify, render_template, request, url_for
from flask_socketio import SocketIO, join_room, disconnect
from sqlalchemy.exc import IntegrityError

from attendance_config import CLASSROOM_RADIUS_METERS, SESSION_DEFAULT_MINUTES
from location_utils import check_classroom_location, parse_coordinate
from pin_utils import current_pin, new_pin_secret, verify_pin

logger = logging.getLogger(__name__)

socketio = SocketIO(cors_allowed_origins="*", async_mode="threading")
_mark_guard = Lock()
_mark_locks = {}


def _mark_lock_for(session_id, student_id):
    key = (int(session_id), int(student_id))
    with _mark_guard:
        lock = _mark_locks.get(key)
        if lock is None:
            lock = Lock()
            _mark_locks[key] = lock
        return lock


def _now():
    return datetime.utcnow()


def expire_stale_sessions(db, AttendanceSession):
    stale = AttendanceSession.query.filter(
        AttendanceSession.is_active.is_(True),
        AttendanceSession.ends_at <= _now(),
    ).all()
    for row in stale:
        row.is_active = False
        row.closed_at = row.closed_at or _now()
    if stale:
        db.session.commit()
    return len(stale)


def session_payload(row, present_count=None, enrolled_count=None, include_pin=False):
    remaining = max(0, int((row.ends_at - _now()).total_seconds())) if row.ends_at else 0
    payload = {
        "id": row.id,
        "course_id": row.course_id,
        "course_name": row.course.name if row.course else None,
        "course_code": row.course.course_id if row.course else None,
        "subject_id": row.subject_id,
        "subject_name": row.subject.name if getattr(row, "subject", None) else None,
        "subject_code": row.subject.code if getattr(row, "subject", None) else None,
        "faculty_id": row.faculty_id,
        "faculty_name": row.faculty.faculty_name if row.faculty else None,
        "started_at": row.started_at.isoformat() + "Z" if row.started_at else None,
        "ends_at": row.ends_at.isoformat() + "Z" if row.ends_at else None,
        "is_active": bool(row.is_active) and remaining > 0 and row.ends_at > _now(),
        "remaining_seconds": remaining,
        "radius_meters": row.radius_meters,
        "present_count": present_count,
        "enrolled_count": enrolled_count,
        "gps_disclaimer": (
            "GPS can be spoofed and cannot guarantee physical presence. "
            "Use PIN + login + location together as a deterrent, not proof."
        ),
    }
    if include_pin and row.pin_secret:
        pin, pin_left, period = current_pin(row.pin_secret, row.id)
        payload["pin"] = pin
        payload["pin_seconds_left"] = pin_left
        payload["pin_period"] = period
    return payload


def init_live_attendance(app, db):
    from models import Attendance, AttendanceSession, Course, Student, Subject

    socketio.init_app(app, cors_allowed_origins="*", async_mode="threading")

    def emit_session_update(sess_row, extra=None):
        enrolled = sess_row.course.students.count() if sess_row.course else 0
        present = Attendance.query.filter_by(session_id=sess_row.id).count()
        payload = session_payload(sess_row, present_count=present, enrolled_count=enrolled)
        if extra:
            payload.update(extra)
        socketio.emit("attendance_update", payload, room=f"session_{sess_row.id}")
        socketio.emit("attendance_update", payload, room=f"course_{sess_row.course_id}")

    @socketio.on("join_session")
    def on_join_session(data):
        from app import faculty_has_course_access, is_admin, is_faculty
        sess_id = (data or {}).get("session_id")
        if not sess_id:
            disconnect()
            return
        if not (is_admin() or is_faculty()):
            disconnect()
            return
        row = AttendanceSession.query.get(sess_id)
        if not row:
            disconnect()
            return
        if not faculty_has_course_access(row.course_id):
            disconnect()
            return
        join_room(f"session_{row.id}")
        join_room(f"course_{row.course_id}")

    @socketio.on("join_student")
    def on_join_student():
        from app import get_current_student, is_student
        if not is_student():
            disconnect()
            return
        student = get_current_student()
        if not student:
            disconnect()
            return
        for course in student.courses:
            join_room(f"course_{course.id}")

    def _request_json():
        if request.is_json:
            return request.get_json(silent=True) or {}
        return request.form

    @app.route("/api/sessions/start", methods=["POST"])
    def api_session_start():
        from app import faculty_has_course_access, get_current_faculty, is_admin, is_faculty
        if not (is_admin() or is_faculty()):
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        expire_stale_sessions(db, AttendanceSession)
        data = _request_json()
        course_id = data.get("course_id")
        if not course_id and data.get("subject_id"):
            sub = Subject.query.get(data.get("subject_id"))
            if sub and sub.course_id:
                course_id = sub.course_id
        if not course_id:
            return jsonify({"status": "error", "message": "Select a subject."}), 400
        if not faculty_has_course_access(course_id):
            return jsonify({"status": "error", "message": "You are not assigned to this subject."}), 403

        course = Course.query.get(course_id)
        if not course:
            return jsonify({"status": "error", "message": "Subject not found."}), 404

        subject = None
        subject_id = data.get("subject_id")
        if subject_id not in (None, ""):
            subject = Subject.query.get(subject_id)
            if subject and not subject.course_id:
                subject.course_id = course.id
                db.session.commit()
        if not subject:
            # Auto-find the subject linked to this course
            subject = Subject.query.filter_by(course_id=course.id).first()
            if not subject:
                subject = Subject.query.filter(
                    db.or_(Subject.code == course.course_id, db.func.lower(Subject.name) == course.name.lower())
                ).first()
                if subject:
                    subject.course_id = course.id
                    db.session.commit()

        lat, lat_err = parse_coordinate(data.get("latitude"), "latitude")
        lng, lng_err = parse_coordinate(data.get("longitude"), "longitude")
        if lat is None:
            lat = course.classroom_lat
        if lng is None:
            lng = course.classroom_lng
        if lat is None or lng is None:
            return jsonify({
                "status": "error",
                "message": (
                    "Classroom location is required. Allow location when starting the session "
                    "or save classroom coordinates on the course."
                ),
            }), 400
        if lat_err and data.get("latitude") not in (None, ""):
            return jsonify({"status": "error", "message": lat_err}), 400
        if lng_err and data.get("longitude") not in (None, ""):
            return jsonify({"status": "error", "message": lng_err}), 400

        try:
            duration = int(data.get("duration_minutes") or SESSION_DEFAULT_MINUTES)
        except (TypeError, ValueError):
            duration = SESSION_DEFAULT_MINUTES
        duration = max(1, min(duration, 180))

        radius = data.get("radius_meters")
        if radius in (None, ""):
            radius = course.classroom_radius_meters or CLASSROOM_RADIUS_METERS
        try:
            radius = float(radius)
        except (TypeError, ValueError):
            radius = CLASSROOM_RADIUS_METERS
        radius = max(10.0, min(radius, 2000.0))

        existing = AttendanceSession.query.filter_by(course_id=course.id, is_active=True).all()
        for row in existing:
            if row.ends_at <= _now():
                row.is_active = False
                row.closed_at = _now()
            else:
                return jsonify({
                    "status": "error",
                    "message": "An attendance session is already active for this course.",
                    "session": session_payload(row, include_pin=True),
                }), 409

        faculty = get_current_faculty()
        row = AttendanceSession(
            course_id=course.id,
            faculty_id=faculty.id if faculty else None,
            started_at=_now(),
            ends_at=_now() + timedelta(minutes=duration),
            is_active=True,
            classroom_lat=lat,
            classroom_lng=lng,
            radius_meters=radius,
            pin_secret=new_pin_secret(),
            subject_id=subject.id if subject else None,
            note=(data.get("note") or "").strip() or None,
        )
        if course.classroom_lat is None:
            course.classroom_lat = lat
            course.classroom_lng = lng
            course.classroom_radius_meters = radius
        db.session.add(row)
        db.session.commit()
        enrolled = course.students.count()
        public = session_payload(row, present_count=0, enrolled_count=enrolled)
        faculty_payload = session_payload(row, present_count=0, enrolled_count=enrolled, include_pin=True)
        socketio.emit("session_started", public, room=f"course_{course.id}")
        socketio.emit("session_started", faculty_payload, room=f"session_{row.id}")
        return jsonify({
            "status": "success",
            "message": "Attendance session started. Share the rotating PIN with students in class.",
            "session": faculty_payload,
        })

    @app.route("/api/sessions/<int:session_id>/stop", methods=["POST"])
    def api_session_stop(session_id):
        from app import faculty_has_course_access, is_admin, is_faculty
        if not (is_admin() or is_faculty()):
            return jsonify({"status": "error", "message": "Unauthorized"}), 401
        row = AttendanceSession.query.get_or_404(session_id)
        if not faculty_has_course_access(row.course_id):
            return jsonify({"status": "error", "message": "Unauthorized"}), 403
        row.is_active = False
        row.closed_at = _now()
        if row.ends_at > _now():
            row.ends_at = _now()
        db.session.commit()
        emit_session_update(row, extra={"closed": True})
        return jsonify({"status": "success", "message": "Attendance session closed.", "session": session_payload(row)})

    @app.route("/api/sessions/<int:session_id>/pin")
    def api_session_pin(session_id):
        from app import faculty_has_course_access, is_admin, is_faculty
        if not (is_admin() or is_faculty()):
            return jsonify({"status": "error", "message": "Unauthorized"}), 401
        expire_stale_sessions(db, AttendanceSession)
        row = AttendanceSession.query.get_or_404(session_id)
        if not faculty_has_course_access(row.course_id):
            return jsonify({"status": "error", "message": "Unauthorized"}), 403
        if not row.is_active or row.ends_at <= _now():
            return jsonify({"status": "error", "message": "Session is not active.", "session": session_payload(row)}), 400
        pin, left, period = current_pin(row.pin_secret, row.id)
        return jsonify({
            "status": "ok",
            "pin": pin,
            "pin_seconds_left": left,
            "pin_period": period,
            "session": session_payload(row, include_pin=True),
        })

    @app.route("/api/student/active_session")
    def api_student_active_session():
        from app import get_current_student, is_student
        if not is_student():
            return jsonify({"status": "error", "message": "Unauthorized"}), 401
        student = get_current_student()
        expire_stale_sessions(db, AttendanceSession)
        course_ids = [c.id for c in student.courses]
        if not course_ids:
            return jsonify({"status": "ok", "session": None, "already_marked": False})
        row = (
            AttendanceSession.query.filter(
                AttendanceSession.course_id.in_(course_ids),
                AttendanceSession.is_active.is_(True),
                AttendanceSession.ends_at > _now(),
            )
            .order_by(AttendanceSession.started_at.desc())
            .first()
        )
        already = False
        if row:
            already = Attendance.query.filter_by(session_id=row.id, student_id=student.id).first() is not None
        payload = session_payload(row) if row else None
        if payload:
            payload.pop("pin", None)
        return jsonify({
            "status": "ok",
            "session": payload,
            "already_marked": already,
            "mark_url": url_for("student_mark_attendance") if row else None,
        })

    def _session_error(code, message, status=400):
        return jsonify({"status": "error", "code": code, "message": message}), status

    def _eligible_session(student, session_id):
        """Server-side identity, enrollment, subject, and session checks. No PIN, no GPS."""
        try:
            session_id = int(session_id)
        except (TypeError, ValueError):
            return None, _session_error("missing_session", "Missing attendance session.")

        query = AttendanceSession.query.filter_by(id=session_id)
        if db.engine.dialect.name != "sqlite":
            query = query.with_for_update()
        row = query.first()
        if not row or not row.is_active or row.ends_at <= _now():
            return None, _session_error(
                "session_expired",
                "This attendance session is closed or has expired.",
            )

        enrolled_ids = [c.id for c in student.courses]
        if row.course_id not in enrolled_ids:
            return None, _session_error(
                "wrong_class",
                "You are not enrolled in this class.",
                403,
            )

        if row.subject_id:
            subject = row.subject or Subject.query.get(row.subject_id)
            if not subject or (subject.course_id and int(subject.course_id) != int(row.course_id)):
                return None, _session_error(
                    "wrong_subject",
                    "This session's subject is not linked to your class.",
                    403,
                )
        return row, None

    @app.route("/api/student/validate_pin", methods=["POST"])
    def api_student_validate_pin():
        """Check PIN + session + enrollment. Does not mark attendance and does not skip GPS later."""
        from app import get_current_student, is_student
        if not is_student():
            return jsonify({"status": "error", "message": "Unauthorized"}), 401
        student = get_current_student()
        if not student:
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        expire_stale_sessions(db, AttendanceSession)
        data = _request_json()
        row, err = _eligible_session(student, data.get("session_id"))
        if err:
            db.session.rollback()
            return err
        if not verify_pin(data.get("pin"), row.pin_secret, row.id):
            db.session.rollback()
            return _session_error(
                "invalid_pin",
                "Invalid or expired PIN. Ask the faculty for the current code.",
            )
        already = Attendance.query.filter_by(session_id=row.id, student_id=student.id).first()
        db.session.commit()
        if already:
            return jsonify({
                "status": "error",
                "code": "duplicate",
                "message": "Attendance already marked for this session.",
            }), 409
        return jsonify({
            "status": "ok",
            "message": "PIN accepted. Share your location so the server can verify you are in range.",
            "session_id": row.id,
        })

    @app.route("/api/sessions/<int:session_id>/live")
    def api_session_live(session_id):
        from app import faculty_has_course_access, get_current_student, is_admin, is_faculty, is_student
        expire_stale_sessions(db, AttendanceSession)
        row = AttendanceSession.query.get_or_404(session_id)
        include_pin = False
        if is_admin() or (is_faculty() and faculty_has_course_access(row.course_id)):
            include_pin = True
        elif is_student():
            student = get_current_student()
            if not student or row.course_id not in [c.id for c in student.courses]:
                return jsonify({"status": "error", "message": "Unauthorized"}), 403
        else:
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        enrolled_students = list(row.course.students) if row.course else []
        present_rows = Attendance.query.filter_by(session_id=row.id).order_by(Attendance.timestamp.asc()).all()
        present_ids = {a.student_id for a in present_rows}
        present = []
        for a in present_rows:
            present.append({
                "student_id": a.student_id,
                "name": a.student.name if a.student else None,
                "student_code": a.student.student_id if a.student else None,
                "timestamp": a.timestamp.isoformat() if a.timestamp else None,
                "distance_meters": a.distance_meters,
                "gps_accuracy": a.gps_accuracy,
                "status": a.status or "present",
            })
        absent = [
            {"student_id": s.id, "name": s.name, "student_code": s.student_id}
            for s in enrolled_students if s.id not in present_ids
        ]
        return jsonify({
            "status": "ok",
            "session": session_payload(
                row,
                present_count=len(present),
                enrolled_count=len(enrolled_students),
                include_pin=include_pin,
            ),
            "present": present,
            "absent": absent,
            "present_count": len(present),
            "enrolled_count": len(enrolled_students),
        })

    @app.route("/api/sessions/active")
    def api_sessions_active():
        from app import faculty_has_course_access, get_current_faculty, is_admin, is_faculty
        if not (is_admin() or is_faculty()):
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        expire_stale_sessions(db, AttendanceSession)
        course_id = request.args.get("course_id")

        query = AttendanceSession.query.filter(
            AttendanceSession.is_active.is_(True),
            AttendanceSession.ends_at > _now(),
        )
        if course_id:
            try:
                cid = int(course_id)
                if not faculty_has_course_access(cid):
                    return jsonify({"status": "error", "message": "Unauthorized"}), 403
                query = query.filter(AttendanceSession.course_id == cid)
            except (TypeError, ValueError):
                pass
        else:
            if not is_admin():
                fac = get_current_faculty()
                if fac:
                    faculty_course_ids = [c.id for c in fac.courses]
                    query = query.filter(AttendanceSession.course_id.in_(faculty_course_ids))

        row = query.order_by(AttendanceSession.started_at.desc()).first()
        if not row:
            today_records = []
            enrolled_count = 0
            if course_id:
                try:
                    cid = int(course_id)
                    from datetime import date
                    today_start = datetime.combine(date.today(), datetime.min.time())
                    records = Attendance.query.filter(
                        Attendance.course_id == cid,
                        Attendance.timestamp >= today_start
                    ).order_by(Attendance.timestamp.desc()).all()
                    for a in records:
                        today_records.append({
                            "student_id": a.student_id,
                            "name": a.student.name if a.student else None,
                            "student_code": a.student.student_id if a.student else None,
                            "timestamp": a.timestamp.isoformat() if a.timestamp else None,
                            "distance_meters": a.distance_meters,
                            "gps_accuracy": a.gps_accuracy,
                            "status": a.status or "present",
                        })
                    c_obj = Course.query.get(cid)
                    if c_obj:
                        enrolled_count = c_obj.students.count()
                except Exception as e:
                    logger.warning(f"Error fetching today records: {e}")

            return jsonify({
                "status": "ok",
                "session": None,
                "present": today_records,
                "present_count": len(today_records),
                "enrolled_count": enrolled_count,
            })

        enrolled_students = list(row.course.students) if row.course else []
        present_rows = Attendance.query.filter_by(session_id=row.id).order_by(Attendance.timestamp.asc()).all()
        present_ids = {a.student_id for a in present_rows}
        present = []
        for a in present_rows:
            present.append({
                "student_id": a.student_id,
                "name": a.student.name if a.student else None,
                "student_code": a.student.student_id if a.student else None,
                "timestamp": a.timestamp.isoformat() if a.timestamp else None,
                "distance_meters": a.distance_meters,
                "gps_accuracy": a.gps_accuracy,
                "status": a.status or "present",
            })
        absent = [
            {"student_id": s.id, "name": s.name, "student_code": s.student_id}
            for s in enrolled_students if s.id not in present_ids
        ]
        return jsonify({
            "status": "ok",
            "session": session_payload(
                row,
                present_count=len(present),
                enrolled_count=len(enrolled_students),
                include_pin=True,
            ),
            "present": present,
            "absent": absent,
            "present_count": len(present),
            "enrolled_count": len(enrolled_students),
        })

    @app.route("/api/student/mark_attendance", methods=["POST"])
    def api_student_mark_attendance():
        from app import get_current_student, is_student
        if not is_student():
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        expire_stale_sessions(db, AttendanceSession)
        student = get_current_student()
        if not student:
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        data = _request_json()
        raw_sess_id = data.get("session_id")
        try:
            sess_key = int(raw_sess_id)
        except (TypeError, ValueError):
            sess_key = 0

        mark_lock = _mark_lock_for(sess_key, student.id)
        with mark_lock:
            row, err = _eligible_session(student, raw_sess_id)
            if err:
                db.session.rollback()
                return err

            if not verify_pin(data.get("pin"), row.pin_secret, row.id):
                db.session.rollback()
                return jsonify({
                    "status": "error",
                    "code": "invalid_pin",
                    "message": "Invalid or expired PIN. Ask the faculty for the current code.",
                }), 400

            existing = Attendance.query.filter_by(session_id=row.id, student_id=student.id).first()
            if existing:
                db.session.rollback()
                return jsonify({
                    "status": "error",
                    "code": "duplicate",
                    "message": "Attendance already marked for this session.",
                }), 409

            lat, lat_err = parse_coordinate(data.get("latitude"), "latitude")
            lng, lng_err = parse_coordinate(data.get("longitude"), "longitude")
            if lat_err or lng_err or lat is None or lng is None:
                db.session.rollback()
                return jsonify({
                    "status": "error",
                    "code": "location_missing",
                    "message": lat_err or lng_err or "Location is required after PIN validation.",
                }), 400

            # Never trust browser flags or precomputed distances.
            for client_flag in ("location_verified", "distance_ok", "verified", "present"):
                if data.get(client_flag):
                    logger.info("Ignored client verification flag %s from student %s", client_flag, student.id)

            loc = check_classroom_location(
                lat, lng,
                row.classroom_lat, row.classroom_lng,
                radius_meters=row.radius_meters,
                accuracy=data.get("accuracy"),
            )
            if not loc.get("ok"):
                db.session.rollback()
                return jsonify({
                    "status": "error",
                    "code": loc.get("code") or "outside_classroom",
                    "message": loc.get("message"),
                    "distance_m": loc.get("distance_m"),
                    "allowed_m": loc.get("allowed_m"),
                    "accuracy_m": loc.get("accuracy_m"),
                }), 400

            try:
                accuracy_val = float(data.get("accuracy")) if data.get("accuracy") not in (None, "") else None
            except (TypeError, ValueError):
                accuracy_val = None

            record = Attendance(
                student_id=student.id,
                course_id=row.course_id,
                timestamp=_now(),
                session_id=row.id,
                latitude=lat,
                longitude=lng,
                gps_accuracy=accuracy_val,
                distance_meters=loc.get("distance_m"),
                status="present",
            )
            db.session.add(record)
            try:
                db.session.flush()
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                return jsonify({
                    "status": "error",
                    "code": "duplicate",
                    "message": "Attendance already marked for this session.",
                }), 409

        emit_session_update(row, extra={
            "new_present": {
                "student_id": student.id,
                "name": student.name,
                "student_code": student.student_id,
                "timestamp": record.timestamp.isoformat(),
            }
        })
        return jsonify({
            "status": "success",
            "message": f"Present — {student.name} marked for {row.course.name}.",
            "distance_m": loc.get("distance_m"),
        })

    @app.route("/student/mark_attendance")
    def student_mark_attendance():
        from app import get_current_student, is_student
        from flask import flash, redirect
        if not is_student():
            return redirect(url_for("student_login"))
        student = get_current_student()
        expire_stale_sessions(db, AttendanceSession)
        course_ids = [c.id for c in student.courses]
        row = None
        if course_ids:
            row = (
                AttendanceSession.query.filter(
                    AttendanceSession.course_id.in_(course_ids),
                    AttendanceSession.is_active.is_(True),
                    AttendanceSession.ends_at > _now(),
                )
                .order_by(AttendanceSession.started_at.desc())
                .first()
            )
        if not row:
            flash("No active attendance session for your classes right now.", "warning")
            return redirect(url_for("student_dashboard"))
        already = Attendance.query.filter_by(session_id=row.id, student_id=student.id).first()
        return render_template(
            "student_mark_attendance.html",
            student=student,
            live_session=session_payload(row),
            already_marked=bool(already),
        )

    logger.info("Live PIN+GPS attendance routes registered")
    return socketio
