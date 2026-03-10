import base64
import csv
import hmac
import io
import logging
import math
import os
import re
import secrets
import shutil
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import (
    Response,
    Flask,
    flash,
    g,
    has_request_context,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    stream_with_context,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from markupsafe import Markup
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import joinedload
from sqlalchemy.pool import NullPool
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "attendance.db"

app = Flask(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return default


APP_ENV = (os.environ.get("APP_ENV") or "").strip().lower()
FLASK_ENV = (os.environ.get("FLASK_ENV") or "").strip().lower()
VERCEL_ENV = (os.environ.get("VERCEL_ENV") or "").strip().lower()
IS_PRODUCTION = APP_ENV == "production" or FLASK_ENV == "production" or VERCEL_ENV == "production"

DEFAULT_SECRET_KEY = "change-this-in-production"
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", DEFAULT_SECRET_KEY)
if IS_PRODUCTION and app.config["SECRET_KEY"] == DEFAULT_SECRET_KEY:
    fallback_entropy = (os.environ.get("DATABASE_URL") or "").strip()
    if fallback_entropy:
        derived_secret = hmac.new(
            b"attendflow-secret-fallback",
            fallback_entropy.encode("utf-8"),
            digestmod="sha256",
        ).hexdigest()
        app.config["SECRET_KEY"] = derived_secret
        app.logger.critical(
            "SECRET_KEY missing in production. Using temporary fallback derived from DATABASE_URL. "
            "Set SECRET_KEY explicitly in environment variables."
        )
    else:
        app.config["SECRET_KEY"] = secrets.token_urlsafe(48)
        app.logger.critical(
            "SECRET_KEY missing in production and DATABASE_URL not set. Using ephemeral fallback key. "
            "Sessions may reset across cold starts. Set SECRET_KEY explicitly."
        )

app.config["MAX_CONTENT_LENGTH"] = max(1, _env_int("MAX_CONTENT_LENGTH_MB", 7)) * 1024 * 1024
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
app.config["SESSION_COOKIE_SECURE"] = _env_bool("SESSION_COOKIE_SECURE", IS_PRODUCTION)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
    hours=max(1, _env_int("SESSION_TTL_HOURS", 12))
)

APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Asia/Kolkata")
try:
    LOCAL_TZ = ZoneInfo(APP_TIMEZONE)
except Exception:
    LOCAL_TZ = timezone.utc


def _database_url() -> str:
    env_url = os.environ.get("DATABASE_URL", "").strip()
    if not env_url:
        if os.environ.get("VERCEL") == "1":
            db_path = _resolve_serverless_sqlite_path()
            return f"sqlite:///{db_path.as_posix()}"
        db_path = _resolve_sqlite_path()
        return f"sqlite:///{db_path.as_posix()}"
    if env_url.startswith("postgres://"):
        env_url = f"postgresql://{env_url[len('postgres://'):]}"
    if env_url.startswith("postgresql://"):
        env_url = f"postgresql+psycopg://{env_url[len('postgresql://'):]}"
    return env_url


def _is_writable_sqlite_path(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    if path.exists():
        try:
            handle = path.open("r+b")
            handle.close()
            return True
        except OSError:
            return False

    try:
        handle = path.open("w+b")
        handle.close()
        path.unlink(missing_ok=True)
        return True
    except OSError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _resolve_serverless_sqlite_path() -> Path:
    preferred_path = Path("/tmp/attendance.db")
    if _is_writable_sqlite_path(preferred_path):
        return preferred_path

    fallback_path = Path(tempfile.gettempdir()) / "attendance.db"
    if _is_writable_sqlite_path(fallback_path):
        app.logger.warning(
            "Preferred serverless DB path %s is not writable. Falling back to %s.",
            preferred_path,
            fallback_path,
        )
        return fallback_path

    raise RuntimeError(
        "No writable serverless SQLite path found. Set DATABASE_URL or SQLITE_DB_PATH."
    )


def _resolve_sqlite_path() -> Path:
    configured_path = (os.environ.get("SQLITE_DB_PATH") or "").strip()
    if configured_path:
        selected = Path(configured_path).expanduser()
        if not _is_writable_sqlite_path(selected):
            raise RuntimeError(
                "Configured SQLITE_DB_PATH is not writable. "
                "Fix permissions or provide a writable DATABASE_URL."
            )
        return selected

    source_path = DB_PATH if DB_PATH.exists() else None
    candidates = [DB_PATH, BASE_DIR / "attendance.db", Path(tempfile.gettempdir()) / "attendance.db"]

    for candidate in candidates:
        if not _is_writable_sqlite_path(candidate):
            continue
        if source_path and candidate != source_path and not candidate.exists():
            try:
                shutil.copy2(source_path, candidate)
            except OSError:
                app.logger.warning(
                    "Could not copy existing DB from %s to %s. Starting with a fresh file.",
                    source_path,
                    candidate,
                )
        if candidate != DB_PATH:
            app.logger.warning(
                "Default DB path %s is not writable. Falling back to %s.",
                DB_PATH,
                candidate,
            )
        return candidate

    raise RuntimeError(
        "No writable SQLite path found. Set DATABASE_URL or SQLITE_DB_PATH to a writable location."
    )


database_uri = _database_url()
app.config["SQLALCHEMY_DATABASE_URI"] = database_uri
if database_uri.startswith("sqlite"):
    sqlite_busy_timeout = max(5, _env_int("SQLITE_BUSY_TIMEOUT_SECONDS", 30))
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "connect_args": {
            "check_same_thread": False,
            "timeout": float(sqlite_busy_timeout),
        },
    }
else:
    if os.environ.get("VERCEL") == "1":
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_pre_ping": True,
            "poolclass": NullPool,
        }
    else:
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_pre_ping": True,
            "pool_recycle": 1800,
            "pool_timeout": 30,
        }

if _env_bool("TRUST_PROXY_HEADERS", True):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

log_level_name = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
log_level = getattr(logging, log_level_name, logging.INFO)
logging.basicConfig(level=log_level)
app.logger.setLevel(log_level)
CSP_ENABLED = not _env_bool("DISABLE_CSP", False)

db = SQLAlchemy(app)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    username = db.Column(db.String(80), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="user")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    company_admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    user_limit = db.Column(db.Integer, nullable=False, default=0)
    analytics_years_limit = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    attendance_entries = db.relationship(
        "Attendance", backref="submitted_by", lazy=True, cascade="all, delete-orphan"
    )
    profile = db.relationship(
        "EmployeeProfile",
        uselist=False,
        back_populates="user",
        cascade="all, delete-orphan",
    )


class EmployeeProfile(db.Model):
    __tablename__ = "employee_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)
    employee_code = db.Column(db.String(40), unique=True, index=True)
    department = db.Column(db.String(80))
    designation = db.Column(db.String(80))
    phone = db.Column(db.String(40))
    joining_date = db.Column(db.Date)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User", back_populates="profile")


class OrganizationSettings(db.Model):
    __tablename__ = "organization_settings"

    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(120), nullable=False, default="AttendFlow")
    shift_start = db.Column(db.String(5), nullable=False, default="09:30")
    shift_end = db.Column(db.String(5), nullable=False, default="18:30")
    grace_minutes = db.Column(db.Integer, nullable=False, default=15)
    full_day_hours = db.Column(db.Float, nullable=False, default=8.0)
    geofence_lat = db.Column(db.Float)
    geofence_lng = db.Column(db.Float)
    geofence_radius_m = db.Column(db.Integer, nullable=False, default=300)
    geofence_enforced = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class Attendance(db.Model):
    __tablename__ = "attendance"
    __table_args__ = (
        db.Index("idx_attendance_user_created_at", "user_id", "created_at"),
        db.Index(
            "idx_attendance_user_event_created_at", "user_id", "event_type", "created_at"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    person_name = db.Column(db.String(120), nullable=False)
    photo_data = db.Column(db.Text)
    photo_path = db.Column(db.String(255))
    event_type = db.Column(db.String(10), nullable=False, default="IN", index=True)
    entry_source = db.Column(db.String(20), nullable=False, default="LIVE", index=True)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    location_text = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class AttendanceCorrection(db.Model):
    __tablename__ = "attendance_corrections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    request_type = db.Column(db.String(20), nullable=False)
    proposed_event_type = db.Column(db.String(10), nullable=False)
    requested_datetime = db.Column(db.DateTime, nullable=False, index=True)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="PENDING", index=True)
    admin_note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime)
    resolved_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    requester = db.relationship("User", foreign_keys=[user_id])
    resolver = db.relationship("User", foreign_keys=[resolved_by_id])


ALLOWED_CORRECTION_TYPES = {"MISSING_IN", "MISSING_OUT", "TIME_FIX"}
SAFE_HTTP_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
CSRF_SESSION_KEY = "_csrf_token"
CSRF_FORM_FIELD = "_csrf_token"
CSRF_HEADER = "X-CSRF-Token"
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
LOGIN_RATE_LIMIT_WINDOW_SECONDS = max(60, _env_int("LOGIN_RATE_LIMIT_WINDOW_SECONDS", 300))
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = max(3, _env_int("LOGIN_RATE_LIMIT_MAX_ATTEMPTS", 12))
_failed_login_attempts: dict[str, list[float]] = {}
STARTUP_ERROR_MESSAGE = ""


def is_json_like_request() -> bool:
    accept = (request.headers.get("Accept") or "").lower()
    return (
        request.is_json
        or "application/json" in accept
        or request.path.startswith("/attendance")
        or request.path.startswith("/admin/analytics")
        or request.path.startswith("/healthz")
        or request.path.startswith("/readyz")
    )


def _get_client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    return request.remote_addr or "unknown"


def _prune_login_attempts(now_ts: float) -> None:
    stale_before = now_ts - LOGIN_RATE_LIMIT_WINDOW_SECONDS
    for client_ip, attempts in list(_failed_login_attempts.items()):
        fresh_attempts = [attempt_ts for attempt_ts in attempts if attempt_ts >= stale_before]
        if fresh_attempts:
            _failed_login_attempts[client_ip] = fresh_attempts
        else:
            _failed_login_attempts.pop(client_ip, None)


def is_login_rate_limited(client_ip: str) -> bool:
    now_ts = datetime.utcnow().timestamp()
    _prune_login_attempts(now_ts)
    attempts = _failed_login_attempts.get(client_ip, [])
    return len(attempts) >= LOGIN_RATE_LIMIT_MAX_ATTEMPTS


def record_login_failure(client_ip: str) -> None:
    now_ts = datetime.utcnow().timestamp()
    attempts = _failed_login_attempts.get(client_ip, [])
    attempts.append(now_ts)
    _failed_login_attempts[client_ip] = attempts
    _prune_login_attempts(now_ts)


def clear_login_failures(client_ip: str) -> None:
    _failed_login_attempts.pop(client_ip, None)


def csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def csrf_input() -> Markup:
    token = csrf_token()
    return Markup(
        f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{token}" autocomplete="off" />'
    )


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["csrf_input"] = csrf_input


def init_db() -> None:
    with app.app_context():
        try:
            db.create_all()
        except SQLAlchemyError as exc:
            if _is_duplicate_schema_error(exc):
                app.logger.warning(
                    "create_all() hit a duplicate schema object (likely stale types from a previous "
                    "partial migration). Retrying table creation individually. Original error: %s",
                    exc,
                )
                db.session.rollback()
                for table in db.metadata.sorted_tables:
                    try:
                        table.create(db.engine, checkfirst=True)
                    except SQLAlchemyError as table_exc:
                        if _is_duplicate_schema_error(table_exc):
                            app.logger.info("Table %s already exists, skipping.", table.name)
                        else:
                            raise
            else:
                raise
        _run_legacy_migrations()
        _ensure_defaults()


def _is_duplicate_schema_error(error: SQLAlchemyError) -> bool:
    message = str(getattr(error, "orig", error)).lower()
    return (
        "duplicate column name" in message
        or "already exists" in message
        or "duplicate key" in message
    )


def _execute_schema_sql(statement: str, tolerate_duplicates: bool = False) -> None:
    try:
        db.session.execute(text(statement))
        db.session.commit()
    except SQLAlchemyError as exc:
        db.session.rollback()
        if tolerate_duplicates and _is_duplicate_schema_error(exc):
            app.logger.info("Schema statement already applied: %s", statement)
            return
        raise


def _run_legacy_migrations() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())

    if "users" in tables:
        columns = {column["name"] for column in inspector.get_columns("users")}
        if "is_active" not in columns:
            _execute_schema_sql(
                "ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1",
                tolerate_duplicates=True,
            )
        if "created_by_id" not in columns:
            _execute_schema_sql(
                "ALTER TABLE users ADD COLUMN created_by_id INTEGER",
                tolerate_duplicates=True,
            )
        if "company_admin_id" not in columns:
            _execute_schema_sql(
                "ALTER TABLE users ADD COLUMN company_admin_id INTEGER",
                tolerate_duplicates=True,
            )
        if "user_limit" not in columns:
            _execute_schema_sql(
                "ALTER TABLE users ADD COLUMN user_limit INTEGER NOT NULL DEFAULT 0",
                tolerate_duplicates=True,
            )
        if "analytics_years_limit" not in columns:
            _execute_schema_sql(
                "ALTER TABLE users ADD COLUMN analytics_years_limit INTEGER NOT NULL DEFAULT 1",
                tolerate_duplicates=True,
            )
        _execute_schema_sql(
            "CREATE INDEX IF NOT EXISTS idx_users_company_admin_id "
            "ON users (company_admin_id)"
        )
        _execute_schema_sql(
            "CREATE INDEX IF NOT EXISTS idx_users_created_by_id "
            "ON users (created_by_id)"
        )
        _execute_schema_sql(
            "CREATE INDEX IF NOT EXISTS idx_users_role "
            "ON users (role)"
        )
        _execute_schema_sql(
            "CREATE INDEX IF NOT EXISTS idx_users_role_company_admin_id "
            "ON users (role, company_admin_id)"
        )

    if "attendance" in tables:
        columns = {column["name"] for column in inspector.get_columns("attendance")}
        if "photo_data" not in columns:
            _execute_schema_sql(
                "ALTER TABLE attendance ADD COLUMN photo_data TEXT",
                tolerate_duplicates=True,
            )
        if "event_type" not in columns:
            _execute_schema_sql(
                "ALTER TABLE attendance ADD COLUMN event_type TEXT NOT NULL DEFAULT 'IN'",
                tolerate_duplicates=True,
            )
        if "entry_source" not in columns:
            _execute_schema_sql(
                "ALTER TABLE attendance ADD COLUMN entry_source TEXT NOT NULL DEFAULT 'LIVE'",
                tolerate_duplicates=True,
            )

        _execute_schema_sql(
            "CREATE INDEX IF NOT EXISTS idx_attendance_user_created_at "
            "ON attendance (user_id, created_at)"
        )
        _execute_schema_sql(
            "CREATE INDEX IF NOT EXISTS idx_attendance_user_event_created_at "
            "ON attendance (user_id, event_type, created_at)"
        )

    if "attendance_corrections" in tables:
        _execute_schema_sql(
            "CREATE INDEX IF NOT EXISTS idx_corrections_user_status_created_at "
            "ON attendance_corrections (user_id, status, created_at)"
        )
        _execute_schema_sql(
            "CREATE INDEX IF NOT EXISTS idx_corrections_status_created_at "
            "ON attendance_corrections (status, created_at)"
        )


def _normalize_username(value: str, fallback: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized or fallback


def _is_valid_username(value: str) -> bool:
    return bool(USERNAME_PATTERN.fullmatch(value or ""))


def _coerce_positive_limit(raw_value: str, fallback: int) -> int:
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return fallback
    if parsed < 1:
        return fallback
    return parsed


def clamp_analytics_years(value: int | None, fallback: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(fallback)
    return max(1, min(10, parsed))


def _next_available_username(base_value: str) -> str:
    base = _normalize_username(base_value, "user")
    if not User.query.filter_by(username=base).first():
        return base
    suffix = 1
    while True:
        candidate = f"{base}{suffix}"
        if not User.query.filter_by(username=candidate).first():
            return candidate
        suffix += 1


def _ensure_defaults() -> None:
    settings = OrganizationSettings.query.first()
    if not settings:
        settings = OrganizationSettings(company_name=os.environ.get("COMPANY_NAME", "AttendFlow"))
        db.session.add(settings)

    super_admin_username = _normalize_username(
        os.environ.get("DEFAULT_SUPER_ADMIN_USERNAME", "superadmin"), "superadmin"
    )
    super_admin_password = os.environ.get("DEFAULT_SUPER_ADMIN_PASSWORD", "Admin@123")
    super_admin_name = os.environ.get("DEFAULT_SUPER_ADMIN_NAME", "Main Administrator")

    default_company_admin_username = _normalize_username(
        os.environ.get(
            "DEFAULT_COMPANY_ADMIN_USERNAME",
            os.environ.get("DEFAULT_ADMIN_USERNAME", "admin"),
        ),
        "admin",
    )
    default_company_admin_password = os.environ.get(
        "DEFAULT_COMPANY_ADMIN_PASSWORD",
        os.environ.get("DEFAULT_ADMIN_PASSWORD", "Admin@123"),
    )
    default_company_admin_name = os.environ.get(
        "DEFAULT_COMPANY_ADMIN_NAME",
        os.environ.get("DEFAULT_ADMIN_NAME", "Company Administrator"),
    )
    default_company_admin_limit = _coerce_positive_limit(
        os.environ.get("DEFAULT_COMPANY_ADMIN_LIMIT", "50"),
        50,
    )
    default_analytics_years_limit = clamp_analytics_years(
        os.environ.get("DEFAULT_COMPANY_ADMIN_ANALYTICS_YEARS", "1"),
        1,
    )

    super_user = User.query.filter_by(username=super_admin_username).first()
    if not super_user:
        super_user = User(
            full_name=super_admin_name,
            username=super_admin_username,
            password_hash=generate_password_hash(super_admin_password),
            role="super_admin",
            is_active=True,
            user_limit=0,
            analytics_years_limit=10,
        )
        db.session.add(super_user)
        db.session.flush()
    else:
        super_user.role = "super_admin"
        super_user.is_active = True
        super_user.analytics_years_limit = 10

    default_company_admin = User.query.filter_by(username=default_company_admin_username).first()
    if default_company_admin and default_company_admin.role == "super_admin":
        default_company_admin = None

    if not default_company_admin:
        default_company_admin = User.query.filter(User.role == "admin").order_by(User.id.asc()).first()

    if not default_company_admin:
        candidate_username = default_company_admin_username
        if User.query.filter_by(username=candidate_username).first():
            candidate_username = _next_available_username("companyadmin")

        default_company_admin = User(
            full_name=default_company_admin_name,
            username=candidate_username,
            password_hash=generate_password_hash(default_company_admin_password),
            role="admin",
            is_active=True,
            created_by_id=super_user.id if super_user else None,
            user_limit=default_company_admin_limit,
            analytics_years_limit=default_analytics_years_limit,
        )
        db.session.add(default_company_admin)
    else:
        if default_company_admin.role == "user":
            default_company_admin.role = "admin"
        if (default_company_admin.user_limit or 0) < 1:
            default_company_admin.user_limit = default_company_admin_limit
        default_company_admin.analytics_years_limit = clamp_analytics_years(
            default_company_admin.analytics_years_limit, default_analytics_years_limit
        )

    db.session.commit()

    admin_accounts = User.query.filter(User.role == "admin").all()
    for admin_account in admin_accounts:
        if (admin_account.user_limit or 0) < 1:
            admin_account.user_limit = default_company_admin_limit
        admin_account.analytics_years_limit = clamp_analytics_years(
            admin_account.analytics_years_limit, default_analytics_years_limit
        )
    db.session.commit()

    fallback_admin = (
        User.query.filter(User.role == "admin").order_by(User.id.asc()).first()
    )
    if fallback_admin:
        orphan_users = User.query.filter(
            User.role == "user", User.company_admin_id.is_(None)
        ).all()
        for orphan in orphan_users:
            orphan.company_admin_id = fallback_admin.id
        if orphan_users:
            db.session.commit()

    users = User.query.options(joinedload(User.profile)).all()
    added_profile = False
    for user in users:
        if not user.profile:
            db.session.add(EmployeeProfile(user_id=user.id))
            added_profile = True
    if added_profile:
        db.session.commit()


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local_datetime(utc_naive: datetime):
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)


def local_day_range_as_utc(value_utc_naive: datetime):
    utc_aware = value_utc_naive.replace(tzinfo=timezone.utc)
    local_now = utc_aware.astimezone(LOCAL_TZ)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    start_utc = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = local_end.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def local_date_range_as_utc(local_day: date):
    local_start = datetime(
        local_day.year,
        local_day.month,
        local_day.day,
        0,
        0,
        0,
        tzinfo=LOCAL_TZ,
    )
    local_end = local_start + timedelta(days=1)
    start_utc = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = local_end.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def shift_year_month(year: int, month: int, delta_months: int):
    total = (year * 12) + (month - 1) + delta_months
    return total // 12, (total % 12) + 1


def month_range_as_utc(year: int, month: int):
    local_start = datetime(year, month, 1, 0, 0, 0, tzinfo=LOCAL_TZ)
    next_year, next_month = shift_year_month(year, month, 1)
    local_end = datetime(next_year, next_month, 1, 0, 0, 0, tzinfo=LOCAL_TZ)
    start_utc = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = local_end.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


def build_month_windows(now_value: datetime, years: int):
    years = clamp_analytics_years(years, 1)
    local_now = to_local_datetime(now_value)
    month_count = years * 12
    windows = []
    for offset in range(month_count - 1, -1, -1):
        year, month = shift_year_month(local_now.year, local_now.month, -offset)
        start_utc, end_utc = month_range_as_utc(year, month)
        windows.append(
            {
                "year": year,
                "month": month,
                "label": datetime(year, month, 1).strftime("%b %Y"),
                "start_utc": start_utc,
                "end_utc": end_utc,
            }
        )
    return windows


def parse_local_datetime_input(raw_value: str):
    value = (raw_value or "").strip()
    if not value:
        return None
    try:
        local_naive = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None
    local_aware = local_naive.replace(tzinfo=LOCAL_TZ)
    return local_aware.astimezone(timezone.utc).replace(tzinfo=None)


def parse_import_datetime_input(raw_value: str):
    value = (raw_value or "").strip()
    if not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        pass

    for pattern in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
    ):
        try:
            local_naive = datetime.strptime(value, pattern)
            return local_naive.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc).replace(
                tzinfo=None
            )
        except ValueError:
            continue
    return None


def parse_hhmm(value: str, fallback: time):
    raw = (value or "").strip()
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        return fallback


def is_valid_hhmm(value: str) -> bool:
    try:
        datetime.strptime((value or "").strip(), "%H:%M")
        return True
    except ValueError:
        return False


def format_duration(seconds_value: int) -> str:
    total_seconds = max(0, int(seconds_value))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours:02d}:{minutes:02d}"


def format_hours_value(seconds_value: int) -> float:
    return round(max(0, int(seconds_value)) / 3600, 2)


def normalize_event_type(raw_value: str):
    value = (raw_value or "").strip().upper()
    return value if value in {"IN", "OUT"} else None


def format_dt(dt_value):
    if dt_value is None:
        return ""
    if isinstance(dt_value, str):
        return dt_value
    dt_utc = dt_value.replace(tzinfo=timezone.utc)
    local_dt = dt_utc.astimezone(LOCAL_TZ)
    return local_dt.strftime("%Y-%m-%d %H:%M:%S")


def current_user():
    if hasattr(g, "_current_user"):
        return g._current_user

    user_id = session.get("user_id")
    if not user_id:
        g._current_user = None
        return None

    user = db.session.get(User, user_id)
    g._current_user = user
    return user


def get_settings() -> OrganizationSettings:
    if has_request_context() and hasattr(g, "_settings"):
        return g._settings

    settings = OrganizationSettings.query.first()
    if not settings:
        settings = OrganizationSettings(company_name="AttendFlow")
        db.session.add(settings)
        db.session.commit()

    if has_request_context():
        g._settings = settings
    return settings


def get_user_profile(user: User, persist: bool = True):
    if user.profile:
        return user.profile
    profile = EmployeeProfile(user_id=user.id)
    if persist:
        db.session.add(profile)
        db.session.flush()
    return profile


def count_managed_users(company_admin_id: int) -> int:
    return (
        User.query.filter(
            User.role == "user",
            User.company_admin_id == company_admin_id,
        ).count()
    )


def build_admin_quota_snapshot(company_admin: User):
    limit = max(0, int(company_admin.user_limit or 0))
    used = count_managed_users(company_admin.id)
    return {
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used),
        "can_create": used < limit,
    }


def create_employee_with_profile(
    admin_user: User,
    full_name: str,
    username: str,
    password: str,
    employee_code: str = "",
    department: str = "",
    designation: str = "",
    phone: str = "",
    joining_date: date | None = None,
) -> User:
    employee_account = User(
        full_name=full_name,
        username=username,
        password_hash=generate_password_hash(password),
        role="user",
        is_active=True,
        created_by_id=admin_user.id,
        company_admin_id=admin_user.id,
        user_limit=0,
    )
    db.session.add(employee_account)
    db.session.flush()
    db.session.add(
        EmployeeProfile(
            user_id=employee_account.id,
            employee_code=employee_code or None,
            department=department or None,
            designation=designation or None,
            phone=phone or None,
            joining_date=joining_date,
            updated_at=now_utc(),
        )
    )
    return employee_account


def parse_joining_date_or_none(raw_value: str) -> date | None:
    cleaned = (raw_value or "").strip()
    if not cleaned:
        return None
    return datetime.strptime(cleaned, "%Y-%m-%d").date()


def _chunked(values: list[int], size: int):
    chunk_size = max(1, int(size))
    for index in range(0, len(values), chunk_size):
        yield values[index : index + chunk_size]


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_earth_m = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius_earth_m * c


def evaluate_geofence(latitude: float, longitude: float, settings: OrganizationSettings):
    if settings.geofence_lat is None or settings.geofence_lng is None:
        if settings.geofence_enforced:
            return False, "Geofence is enforced but company coordinates are not configured.", None
        return True, "", None

    radius = max(1, int(settings.geofence_radius_m or 300))
    distance = haversine_meters(
        latitude, longitude, settings.geofence_lat, settings.geofence_lng
    )

    if settings.geofence_enforced and distance > radius:
        return (
            False,
            f"You are outside office geofence by {int(distance - radius)} meters.",
            distance,
        )
    return True, "", distance


def compute_late_minutes(first_in_utc, settings: OrganizationSettings) -> int:
    if not first_in_utc:
        return 0

    shift_start_time = parse_hhmm(settings.shift_start, time(9, 30))
    first_local = to_local_datetime(first_in_utc)
    shift_local = datetime.combine(first_local.date(), shift_start_time, tzinfo=LOCAL_TZ)
    grace_end = shift_local + timedelta(minutes=max(0, int(settings.grace_minutes or 0)))

    if first_local <= grace_end:
        return 0
    return int((first_local - grace_end).total_seconds() // 60)


def get_latest_event_for_user(user_id: int):
    return (
        Attendance.query.filter_by(user_id=user_id)
        .order_by(Attendance.created_at.desc(), Attendance.id.desc())
        .first()
    )


def build_window_events_for_user(user_id: int, start_utc: datetime, end_utc: datetime):
    previous_event = (
        Attendance.query.filter(
            Attendance.user_id == user_id, Attendance.created_at < start_utc
        )
        .order_by(Attendance.created_at.desc(), Attendance.id.desc())
        .first()
    )

    events_in_window = (
        Attendance.query.filter(
            Attendance.user_id == user_id,
            Attendance.created_at >= start_utc,
            Attendance.created_at < end_utc,
        )
        .order_by(Attendance.created_at.asc(), Attendance.id.asc())
        .all()
    )

    events = []
    if previous_event:
        events.append(previous_event)
    events.extend(events_in_window)
    return events


def compute_work_seconds_from_events(events, start_utc: datetime, end_utc: datetime) -> int:
    total_seconds = 0
    open_in = None

    for event in events:
        timestamp = event.created_at
        if timestamp >= end_utc:
            break

        if event.event_type == "IN":
            if open_in is None:
                open_in = timestamp
            continue

        if event.event_type == "OUT" and open_in is not None:
            segment_start = max(open_in, start_utc)
            segment_end = min(timestamp, end_utc)
            if segment_end > segment_start:
                total_seconds += int((segment_end - segment_start).total_seconds())
            open_in = None

    if open_in is not None:
        segment_start = max(open_in, start_utc)
        if end_utc > segment_start:
            total_seconds += int((end_utc - segment_start).total_seconds())

    return total_seconds


def summarize_user_day(user_id: int, local_day: date, upto_utc: datetime, settings: OrganizationSettings):
    day_start_utc, day_end_utc = local_date_range_as_utc(local_day)
    events = build_window_events_for_user(user_id, day_start_utc, min(day_end_utc, upto_utc))
    return summarize_user_day_from_events(events, local_day, upto_utc, settings)


def summarize_user_day_from_events(
    events,
    local_day: date,
    upto_utc: datetime,
    settings: OrganizationSettings,
):
    day_start_utc, day_end_utc = local_date_range_as_utc(local_day)
    effective_end = min(day_end_utc, upto_utc)

    work_seconds = compute_work_seconds_from_events(events, day_start_utc, effective_end)

    day_events = [
        event
        for event in events
        if event.created_at >= day_start_utc and event.created_at < effective_end
    ]

    first_in = next((event for event in day_events if event.event_type == "IN"), None)
    reverse_events = list(reversed(day_events))
    last_out = next((event for event in reverse_events if event.event_type == "OUT"), None)

    late_minutes = compute_late_minutes(first_in.created_at if first_in else None, settings)
    target_seconds = int(max(0.0, float(settings.full_day_hours or 8.0)) * 3600)
    overtime_seconds = max(0, work_seconds - target_seconds)

    return {
        "work_seconds": work_seconds,
        "work_hours": format_hours_value(work_seconds),
        "work_hours_hhmm": format_duration(work_seconds),
        "events_count": len(day_events),
        "first_in": format_dt(first_in.created_at) if first_in else "--",
        "last_out": format_dt(last_out.created_at) if last_out else "--",
        "late_minutes": late_minutes,
        "overtime_seconds": overtime_seconds,
        "overtime_hours": round(overtime_seconds / 3600, 2),
    }


def load_events_for_users_window(
    user_ids: list[int], start_utc: datetime, end_utc: datetime
):
    if not user_ids:
        return {}, []

    normalized_ids = sorted({int(user_id) for user_id in user_ids})
    events_by_user = {user_id: [] for user_id in normalized_ids}

    window_events = []
    for batch in _chunked(normalized_ids, 800):
        previous_events_subq = (
            db.session.query(
                Attendance.user_id.label("user_id"),
                db.func.max(Attendance.id).label("max_id"),
            )
            .filter(
                Attendance.user_id.in_(batch),
                Attendance.created_at < start_utc,
            )
            .group_by(Attendance.user_id)
            .subquery()
        )

        previous_events = (
            db.session.query(Attendance)
            .join(previous_events_subq, Attendance.id == previous_events_subq.c.max_id)
            .all()
        )
        for event in previous_events:
            events_by_user.setdefault(event.user_id, []).append(event)

        batch_window_events = (
            Attendance.query.filter(
                Attendance.user_id.in_(batch),
                Attendance.created_at >= start_utc,
                Attendance.created_at < end_utc,
            )
            .order_by(Attendance.user_id.asc(), Attendance.created_at.asc(), Attendance.id.asc())
            .all()
        )
        window_events.extend(batch_window_events)
        for event in batch_window_events:
            events_by_user.setdefault(event.user_id, []).append(event)

    window_events.sort(key=lambda item: (item.user_id, item.created_at, item.id))

    return events_by_user, window_events


def correction_to_dict(correction: AttendanceCorrection):
    requester = correction.requester
    resolver = correction.resolver
    return {
        "id": correction.id,
        "request_type": correction.request_type,
        "proposed_event_type": correction.proposed_event_type,
        "requested_datetime": format_dt(correction.requested_datetime),
        "reason": correction.reason,
        "status": correction.status,
        "admin_note": correction.admin_note or "",
        "created_at": format_dt(correction.created_at),
        "resolved_at": format_dt(correction.resolved_at) if correction.resolved_at else "",
        "requester_name": requester.full_name if requester else "Deleted User",
        "requester_username": requester.username if requester else "deleted",
        "resolver_name": resolver.full_name if resolver else "",
    }


def attendance_to_dict(entry: Attendance):
    photo_src = entry.photo_data
    if not photo_src and entry.photo_path:
        photo_src = url_for("static", filename=entry.photo_path)

    submitted_by = entry.submitted_by
    if submitted_by:
        user_full_name = submitted_by.full_name
        username = submitted_by.username
    else:
        user_full_name = "Deleted User"
        username = "deleted"

    return {
        "id": entry.id,
        "person_name": entry.person_name,
        "event_type": entry.event_type,
        "entry_source": entry.entry_source,
        "photo_src": photo_src,
        "latitude": entry.latitude,
        "longitude": entry.longitude,
        "location_text": entry.location_text,
        "created_at": format_dt(entry.created_at),
        "user_full_name": user_full_name,
        "username": username,
    }


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please login first.", "warning")
            return redirect(url_for("login"))
        user = current_user()
        if not user:
            stale_uid = session.get("user_id")
            app.logger.warning(
                "login_required: user_id=%s not found in database. "
                "This usually means the database was reset (ephemeral SQLite on serverless). "
                "Set DATABASE_URL to a persistent Postgres database.",
                stale_uid,
            )
            session.clear()
            flash("Session expired. Please login again.", "warning")
            return redirect(url_for("login"))
        if not user.is_active:
            session.clear()
            flash("Account is inactive. Contact administrator.", "danger")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            flash("Please login first.", "warning")
            return redirect(url_for("login"))
        if user.role != "admin":
            flash("Company admin access required.", "danger")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)

    return wrapper


def super_admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            flash("Please login first.", "warning")
            return redirect(url_for("login"))
        if user.role != "super_admin":
            flash("Main admin access required.", "danger")
            return redirect(url_for("dashboard"))
        return fn(*args, **kwargs)

    return wrapper


@app.before_request
def block_on_startup_failure():
    if not STARTUP_ERROR_MESSAGE:
        return None
    if is_json_like_request():
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Service initialization failed. Check server logs and environment variables.",
                }
            ),
            503,
        )
    return Response(
        "Service initialization failed. Check server logs and environment variables.",
        status=503,
        mimetype="text/plain",
    )


@app.before_request
def enforce_csrf_protection():
    if request.method in SAFE_HTTP_METHODS:
        return None
    if request.endpoint == "static":
        return None

    expected_token = session.get(CSRF_SESSION_KEY, "")
    provided_token = request.form.get(CSRF_FORM_FIELD, "")
    if not provided_token:
        provided_token = request.headers.get(CSRF_HEADER, "")

    if not expected_token or not provided_token:
        app.logger.warning("CSRF validation failed: missing token for %s %s", request.method, request.path)
        message = "Security token missing. Refresh and retry."
        if is_json_like_request():
            return jsonify({"ok": False, "error": message}), 400
        flash(message, "danger")
        redirect_target = request.referrer or (
            url_for("dashboard") if session.get("user_id") else url_for("login")
        )
        return redirect(redirect_target)

    if not hmac.compare_digest(str(expected_token), str(provided_token)):
        app.logger.warning("CSRF validation failed: invalid token for %s %s", request.method, request.path)
        message = "Security token invalid or expired. Refresh and retry."
        if is_json_like_request():
            return jsonify({"ok": False, "error": message}), 400
        flash(message, "danger")
        redirect_target = request.referrer or (
            url_for("dashboard") if session.get("user_id") else url_for("login")
        )
        return redirect(redirect_target)

    return None


@app.after_request
def set_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(self)")
    if request.is_secure:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )

    if CSP_ENABLED:
        csp_policy = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' https://nominatim.openstreetmap.org; "
            "object-src 'none'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers.setdefault("Content-Security-Policy", csp_policy)

    if session.get("user_id") and request.method == "GET" and not request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        response.headers.setdefault("Pragma", "no-cache")

    return response


@app.errorhandler(413)
def handle_payload_too_large(_error):
    if is_json_like_request():
        return jsonify({"ok": False, "error": "Payload too large."}), 413
    flash("Uploaded file is too large.", "danger")
    return redirect(request.referrer or url_for("dashboard" if session.get("user_id") else "login"))


@app.errorhandler(429)
def handle_too_many_requests(_error):
    if is_json_like_request():
        return jsonify({"ok": False, "error": "Too many requests. Please retry shortly."}), 429
    flash("Too many requests. Please wait a moment and retry.", "danger")
    return redirect(request.referrer or url_for("login"))


@app.errorhandler(500)
def handle_server_error(error):
    app.logger.exception("Unhandled server error on %s %s", request.method, request.path, exc_info=error)
    if is_json_like_request():
        return jsonify({"ok": False, "error": "Internal server error."}), 500
    flash("Unexpected server error. Please retry.", "danger")
    return redirect(request.referrer or url_for("dashboard" if session.get("user_id") else "login"))


@app.context_processor
def inject_template_context():
    company_name = "AttendFlow"
    try:
        settings = OrganizationSettings.query.first()
        if settings and settings.company_name:
            company_name = settings.company_name
    except Exception:
        company_name = "AttendFlow"
    return {"session_user": current_user(), "company_name": company_name}


def _validate_and_normalize_image(image_data: str) -> str:
    if not image_data or "," not in image_data:
        raise ValueError("Invalid image payload.")

    meta, encoded = image_data.split(",", 1)
    meta = meta.strip().lower()
    allowed_prefixes = (
        "data:image/jpeg;base64",
        "data:image/jpg;base64",
        "data:image/png;base64",
        "data:image/webp;base64",
    )

    if not any(meta.startswith(prefix) for prefix in allowed_prefixes):
        raise ValueError("Unsupported image format.")

    try:
        binary = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("Invalid image payload.") from exc

    if len(binary) < 1000:
        raise ValueError("Captured image is too small.")
    if len(binary) > 6 * 1024 * 1024:
        raise ValueError("Captured image is too large.")

    if meta.startswith("data:image/png;base64"):
        mime = "image/png"
    elif meta.startswith("data:image/webp;base64"):
        mime = "image/webp"
    else:
        mime = "image/jpeg"

    clean_base64 = base64.b64encode(binary).decode("ascii")
    return f"data:{mime};base64,{clean_base64}"

def build_admin_analytics_payload(
    now_value: datetime, company_admin_id: int | None = None, years: int = 1
):
    years = clamp_analytics_years(years, 1)
    settings = get_settings()
    local_today = to_local_datetime(now_value).date()

    month_windows = build_month_windows(now_value, years)
    labels = [item["label"] for item in month_windows]
    first_window_start = month_windows[0]["start_utc"]

    employees_query = User.query.options(joinedload(User.profile)).filter(User.role == "user")
    if company_admin_id is not None:
        employees_query = employees_query.filter(User.company_admin_id == company_admin_id)
    employees = employees_query.order_by(User.full_name.asc()).all()
    employee_ids = [employee.id for employee in employees]
    events_by_user, window_events = load_events_for_users_window(
        employee_ids, first_window_start, now_value
    )

    employee_hours = []
    employee_daily_hours = {}
    employee_list = []
    employee_scores = []
    department_hours_map = {}
    period_hours_total = 0.0
    period_late_days_total = 0
    period_overtime_hours_total = 0.0

    month_index_map = {
        (window["year"], window["month"]): index for index, window in enumerate(month_windows)
    }
    total_period_count = max(1, len(month_windows))

    for employee in employees:
        events = events_by_user.get(employee.id, [])
        monthly_hours = []
        events_by_local_day = {}

        for event in events:
            if event.created_at < first_window_start:
                continue
            local_day = to_local_datetime(event.created_at).date()
            events_by_local_day.setdefault(local_day, []).append(event)

        for index, window in enumerate(month_windows):
            period_start = window["start_utc"]
            period_end = window["end_utc"]
            segment_end = min(period_end, now_value) if index == len(month_windows) - 1 else period_end
            seconds = compute_work_seconds_from_events(events, period_start, segment_end)
            monthly_hours.append(round(seconds / 3600, 2))

        total_hours = round(sum(monthly_hours), 2)
        period_hours_total += total_hours
        employee_hours.append({"employee": employee.full_name, "hours": total_hours})
        employee_daily_hours[str(employee.id)] = monthly_hours
        employee_list.append({"id": employee.id, "name": employee.full_name})

        late_days = 0
        per_day_seconds = {}
        for local_day, day_events in events_by_local_day.items():
            key = (local_day.year, local_day.month)
            if key not in month_index_map:
                continue
            sorted_events = sorted(day_events, key=lambda item: item.created_at)
            first_in = next((event for event in sorted_events if event.event_type == "IN"), None)
            if first_in and compute_late_minutes(first_in.created_at, settings) > 0:
                late_days += 1
            day_start_utc, day_end_utc = local_date_range_as_utc(local_day)
            effective_end = min(day_end_utc, now_value)
            per_day_seconds[local_day] = compute_work_seconds_from_events(
                sorted_events, day_start_utc, effective_end
            )

        overtime_seconds = 0
        target_seconds = int(max(0.0, float(settings.full_day_hours or 8.0)) * 3600)
        for seconds in per_day_seconds.values():
            overtime_seconds += max(0, seconds - target_seconds)

        present_periods = sum(1 for value in monthly_hours if value > 0)
        punctuality = 100.0
        if present_periods > 0:
            punctuality = round(max(0.0, 100.0 - (late_days / max(1, len(per_day_seconds))) * 100.0), 1)

        attendance_ratio = round((present_periods / total_period_count) * 100.0, 1)
        performance_score = round((attendance_ratio * 0.55) + (punctuality * 0.45), 1)
        employee_scores.append(
            {
                "employee": employee.full_name,
                "score": performance_score,
                "late_days": late_days,
                "present_days": len(per_day_seconds),
                "hours": total_hours,
            }
        )
        period_late_days_total += late_days
        period_overtime_hours_total += round(overtime_seconds / 3600, 2)

        profile = employee.profile or get_user_profile(employee, persist=False)
        department = profile.department if profile and profile.department else "Unassigned"
        department_hours_map[department] = round(
            department_hours_map.get(department, 0.0) + total_hours,
            2,
        )

    employee_hours.sort(key=lambda item: item["hours"], reverse=True)
    employee_scores.sort(key=lambda item: item["score"], reverse=True)

    count_map = {(window["year"], window["month"]): {"IN": 0, "OUT": 0} for window in month_windows}
    for event in window_events:
        local_dt = to_local_datetime(event.created_at)
        key = (local_dt.year, local_dt.month)
        if key in count_map and event.event_type in {"IN", "OUT"}:
            count_map[key][event.event_type] += 1

    daily_in_out = []
    for window in month_windows:
        key = (window["year"], window["month"])
        daily_in_out.append(
            {
                "date": window["label"],
                "in_count": count_map[key]["IN"],
                "out_count": count_map[key]["OUT"],
            }
        )

    department_hours = [
        {"department": key, "hours": value}
        for key, value in sorted(
            department_hours_map.items(), key=lambda item: item[1], reverse=True
        )
    ]

    today_user_count = max(1, len(employees))
    today_hours_sum = 0.0
    late_today = 0
    overtime_today = 0.0
    for employee in employees:
        summary = summarize_user_day_from_events(
            events_by_user.get(employee.id, []), local_today, now_value, settings
        )
        today_hours_sum += summary["work_hours"]
        if summary["late_minutes"] > 0:
            late_today += 1
        overtime_today += summary["overtime_hours"]

    pending_corrections_query = AttendanceCorrection.query.filter(
        AttendanceCorrection.status == "PENDING"
    )
    if company_admin_id is not None:
        pending_corrections_query = pending_corrections_query.join(
            User, AttendanceCorrection.user_id == User.id
        ).filter(User.role == "user", User.company_admin_id == company_admin_id)
    pending_corrections = pending_corrections_query.count()

    return {
        "labels": labels,
        "employees": employee_list,
        "hours_by_employee": employee_hours,
        "employee_daily_hours": employee_daily_hours,
        "daily_in_out": daily_in_out,
        "employee_scores": employee_scores,
        "department_hours": department_hours,
        "kpis": {
            "avg_today_hours": round(today_hours_sum / today_user_count, 2),
            "late_today": late_today,
            "overtime_today": round(overtime_today, 2),
            "pending_corrections": pending_corrections,
            "avg_period_hours": round(period_hours_total / max(1, len(employees)), 2),
            "late_period_days": period_late_days_total,
            "overtime_period": round(period_overtime_hours_total, 2),
        },
        "period": {
            "years": years,
            "label": f"Last {years} year{'s' if years > 1 else ''}",
            "points": len(labels),
        },
    }


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "status": "healthy", "time": now_utc().isoformat() + "Z"})


@app.route("/readyz")
def readyz():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"ok": True, "status": "ready"})
    except SQLAlchemyError:
        app.logger.exception("Readiness check failed")
        return jsonify({"ok": False, "status": "db-unavailable"}), 503


@app.route("/")
def home():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    flash("Self-registration is disabled. Contact your company admin.", "warning")
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        client_ip = _get_client_ip()
        if is_login_rate_limited(client_ip):
            flash("Too many failed login attempts. Please retry in a few minutes.", "danger")
            return render_template("login.html"), 429

        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        if len(username) > 80:
            flash("Invalid username or password.", "danger")
            record_login_failure(client_ip)
            return render_template("login.html"), 400

        user = User.query.filter_by(username=username).first()

        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid username or password.", "danger")
            record_login_failure(client_ip)
            return render_template("login.html"), 401
        if not user.is_active:
            flash("Account is inactive. Contact administrator.", "danger")
            record_login_failure(client_ip)
            return render_template("login.html"), 403

        session.clear()
        clear_login_failures(client_ip)
        session["user_id"] = user.id
        session.permanent = True
        flash(f"Welcome back, {user.full_name}!", "success")
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "info")
    return redirect(url_for("login"))


@app.route("/super-admin")
@login_required
@super_admin_required
def super_admin_dashboard():
    company_admins = User.query.filter(User.role == "admin").order_by(User.created_at.desc()).all()
    usage_rows = (
        db.session.query(User.company_admin_id, db.func.count(User.id))
        .filter(User.role == "user", User.company_admin_id.isnot(None))
        .group_by(User.company_admin_id)
        .all()
    )
    usage_map = {row[0]: int(row[1]) for row in usage_rows}

    company_admin_rows = []
    total_capacity = 0
    total_used = 0
    for admin_user in company_admins:
        limit = max(0, int(admin_user.user_limit or 0))
        analytics_years_limit = clamp_analytics_years(admin_user.analytics_years_limit, 1)
        used = usage_map.get(admin_user.id, 0)
        remaining = max(0, limit - used)
        total_capacity += limit
        total_used += used
        company_admin_rows.append(
            {
                "id": admin_user.id,
                "full_name": admin_user.full_name,
                "username": admin_user.username,
                "is_active": admin_user.is_active,
                "user_limit": limit,
                "analytics_years_limit": analytics_years_limit,
                "used": used,
                "remaining": remaining,
                "created_at": format_dt(admin_user.created_at),
            }
        )

    total_employees = User.query.filter(User.role == "user").count()
    stats = {
        "total_company_admins": len(company_admin_rows),
        "total_employees": total_employees,
        "capacity_total": total_capacity,
        "capacity_used": total_used,
        "capacity_remaining": max(0, total_capacity - total_used),
    }

    return render_template(
        "super_admin_dashboard.html",
        stats=stats,
        company_admin_rows=company_admin_rows,
    )


@app.route("/super-admin/company-admin/create", methods=["POST"])
@login_required
@super_admin_required
def create_company_admin():
    full_name = (request.form.get("full_name") or "").strip()
    username = _normalize_username(request.form.get("username"), "")
    password = request.form.get("password", "")
    user_limit_raw = (request.form.get("user_limit") or "").strip()
    analytics_years_limit_raw = (request.form.get("analytics_years_limit") or "").strip()

    if (
        not full_name
        or not username
        or not password
        or not user_limit_raw
        or not analytics_years_limit_raw
    ):
        flash("Name, username, password, user limit, and analytics years are required.", "danger")
        return redirect(url_for("super_admin_dashboard"))
    if len(full_name) > 120:
        flash("Name is too long.", "danger")
        return redirect(url_for("super_admin_dashboard"))
    if len(username) > 80:
        flash("Username is too long.", "danger")
        return redirect(url_for("super_admin_dashboard"))
    if not _is_valid_username(username):
        flash("Username can use only lowercase letters, numbers, dot, underscore, and hyphen.", "danger")
        return redirect(url_for("super_admin_dashboard"))
    if len(password) < 6:
        flash("Password must be at least 6 characters.", "danger")
        return redirect(url_for("super_admin_dashboard"))
    if len(password) > 255:
        flash("Password is too long.", "danger")
        return redirect(url_for("super_admin_dashboard"))

    try:
        user_limit = int(user_limit_raw)
    except ValueError:
        flash("User limit must be a valid number.", "danger")
        return redirect(url_for("super_admin_dashboard"))
    if user_limit < 1 or user_limit > 50000:
        flash("User limit must be between 1 and 50000.", "danger")
        return redirect(url_for("super_admin_dashboard"))

    try:
        analytics_years_limit = int(analytics_years_limit_raw)
    except ValueError:
        flash("Analytics years must be a valid number.", "danger")
        return redirect(url_for("super_admin_dashboard"))
    if analytics_years_limit < 1 or analytics_years_limit > 10:
        flash("Analytics years must be between 1 and 10.", "danger")
        return redirect(url_for("super_admin_dashboard"))

    if User.query.filter_by(username=username).first():
        flash("Username already exists. Choose another username.", "danger")
        return redirect(url_for("super_admin_dashboard"))

    super_user = current_user()
    admin_account = User(
        full_name=full_name,
        username=username,
        password_hash=generate_password_hash(password),
        role="admin",
        is_active=True,
        created_by_id=super_user.id if super_user else None,
        user_limit=user_limit,
        analytics_years_limit=analytics_years_limit,
    )

    try:
        db.session.add(admin_account)
        db.session.flush()
        db.session.add(EmployeeProfile(user_id=admin_account.id))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Could not create company admin due to duplicate data.", "danger")
        return redirect(url_for("super_admin_dashboard"))
    except SQLAlchemyError:
        db.session.rollback()
        flash("Could not create company admin right now.", "danger")
        return redirect(url_for("super_admin_dashboard"))

    flash("Company admin account created successfully.", "success")
    return redirect(url_for("super_admin_dashboard"))


@app.route("/super-admin/company-admin/<int:admin_id>/limit", methods=["POST"])
@login_required
@super_admin_required
def update_company_admin_limit(admin_id: int):
    admin_account = db.session.get(User, admin_id)
    if not admin_account or admin_account.role != "admin":
        flash("Company admin account not found.", "warning")
        return redirect(url_for("super_admin_dashboard"))

    user_limit_raw = (request.form.get("user_limit") or "").strip()
    try:
        user_limit = int(user_limit_raw)
    except ValueError:
        flash("User limit must be a valid number.", "danger")
        return redirect(url_for("super_admin_dashboard"))
    if user_limit < 1 or user_limit > 50000:
        flash("User limit must be between 1 and 50000.", "danger")
        return redirect(url_for("super_admin_dashboard"))

    used = count_managed_users(admin_account.id)
    admin_account.user_limit = user_limit

    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("Could not update user limit right now.", "danger")
        return redirect(url_for("super_admin_dashboard"))

    if user_limit < used:
        flash(
            "Limit updated. Current usage is above the new limit, so new user creation is blocked until usage drops.",
            "warning",
        )
    else:
        flash("Company admin user limit updated.", "success")
    return redirect(url_for("super_admin_dashboard"))


@app.route("/super-admin/company-admin/<int:admin_id>/analytics-limit", methods=["POST"])
@login_required
@super_admin_required
def update_company_admin_analytics_limit(admin_id: int):
    admin_account = db.session.get(User, admin_id)
    if not admin_account or admin_account.role != "admin":
        flash("Company admin account not found.", "warning")
        return redirect(url_for("super_admin_dashboard"))

    analytics_years_limit_raw = (request.form.get("analytics_years_limit") or "").strip()
    try:
        analytics_years_limit = int(analytics_years_limit_raw)
    except ValueError:
        flash("Analytics years must be a valid number.", "danger")
        return redirect(url_for("super_admin_dashboard"))
    if analytics_years_limit < 1 or analytics_years_limit > 10:
        flash("Analytics years must be between 1 and 10.", "danger")
        return redirect(url_for("super_admin_dashboard"))

    admin_account.analytics_years_limit = analytics_years_limit
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("Could not update analytics years right now.", "danger")
        return redirect(url_for("super_admin_dashboard"))

    flash("Company admin analytics range updated.", "success")
    return redirect(url_for("super_admin_dashboard"))


@app.route("/super-admin/company-admin/<int:admin_id>/status", methods=["POST"])
@login_required
@super_admin_required
def toggle_company_admin_status(admin_id: int):
    admin_account = db.session.get(User, admin_id)
    if not admin_account or admin_account.role != "admin":
        flash("Company admin account not found.", "warning")
        return redirect(url_for("super_admin_dashboard"))

    if admin_account.is_active:
        active_admin_count = User.query.filter(
            User.role == "admin", User.is_active.is_(True)
        ).count()
        if active_admin_count <= 1:
            flash("At least one company admin must stay active.", "danger")
            return redirect(url_for("super_admin_dashboard"))

    admin_account.is_active = not admin_account.is_active
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("Could not update account status right now.", "danger")
        return redirect(url_for("super_admin_dashboard"))

    flash("Company admin status updated.", "success")
    return redirect(url_for("super_admin_dashboard"))


@app.route("/super-admin/company-admin/<int:admin_id>/password", methods=["POST"])
@login_required
@super_admin_required
def reset_company_admin_password(admin_id: int):
    admin_account = db.session.get(User, admin_id)
    if not admin_account or admin_account.role != "admin":
        flash("Company admin account not found.", "warning")
        return redirect(url_for("super_admin_dashboard"))

    new_password = request.form.get("new_password", "")
    if len(new_password) < 6:
        flash("New password must be at least 6 characters.", "danger")
        return redirect(url_for("super_admin_dashboard"))
    if len(new_password) > 255:
        flash("New password is too long.", "danger")
        return redirect(url_for("super_admin_dashboard"))

    admin_account.password_hash = generate_password_hash(new_password)
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("Could not reset password right now.", "danger")
        return redirect(url_for("super_admin_dashboard"))

    flash("Company admin password reset successfully.", "success")
    return redirect(url_for("super_admin_dashboard"))


@app.route("/admin/employee/create", methods=["POST"])
@login_required
@admin_required
def create_employee_account():
    admin_user = current_user()
    quota = build_admin_quota_snapshot(admin_user)
    if not quota["can_create"]:
        flash("User creation limit reached. Contact main admin to increase your quota.", "danger")
        return redirect(url_for("dashboard"))

    full_name = (request.form.get("full_name") or "").strip()
    username = _normalize_username(request.form.get("username"), "")
    password = request.form.get("password", "")
    employee_code = (request.form.get("employee_code") or "").strip().upper()
    department = (request.form.get("department") or "").strip()
    designation = (request.form.get("designation") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    joining_date_raw = (request.form.get("joining_date") or "").strip()

    if not full_name or not username or not password:
        flash("Name, username, and password are required.", "danger")
        return redirect(url_for("dashboard"))
    if len(full_name) > 120:
        flash("Employee name is too long.", "danger")
        return redirect(url_for("dashboard"))
    if len(username) > 80:
        flash("Username is too long.", "danger")
        return redirect(url_for("dashboard"))
    if not _is_valid_username(username):
        flash("Username can use only lowercase letters, numbers, dot, underscore, and hyphen.", "danger")
        return redirect(url_for("dashboard"))
    if len(password) < 6:
        flash("Password must be at least 6 characters.", "danger")
        return redirect(url_for("dashboard"))
    if len(password) > 255:
        flash("Password is too long.", "danger")
        return redirect(url_for("dashboard"))
    if employee_code and len(employee_code) > 40:
        flash("Employee code is too long.", "danger")
        return redirect(url_for("dashboard"))
    if department and len(department) > 80:
        flash("Department is too long.", "danger")
        return redirect(url_for("dashboard"))
    if designation and len(designation) > 80:
        flash("Designation is too long.", "danger")
        return redirect(url_for("dashboard"))
    if phone and len(phone) > 40:
        flash("Phone is too long.", "danger")
        return redirect(url_for("dashboard"))

    if User.query.filter_by(username=username).first():
        flash("Username already exists. Choose another username.", "danger")
        return redirect(url_for("dashboard"))

    try:
        joining_date = parse_joining_date_or_none(joining_date_raw)
    except ValueError:
        flash("Joining date format is invalid.", "danger")
        return redirect(url_for("dashboard"))

    try:
        create_employee_with_profile(
            admin_user=admin_user,
            full_name=full_name,
            username=username,
            password=password,
            employee_code=employee_code,
            department=department,
            designation=designation,
            phone=phone,
            joining_date=joining_date,
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Username or employee code already exists.", "danger")
        return redirect(url_for("dashboard"))
    except SQLAlchemyError:
        db.session.rollback()
        flash("Could not create employee account right now.", "danger")
        return redirect(url_for("dashboard"))

    flash("Employee account created successfully.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/employee/import-template")
@login_required
@admin_required
def download_employee_import_template():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "full_name",
            "username",
            "password",
            "employee_code",
            "department",
            "designation",
            "phone",
            "joining_date",
        ]
    )
    writer.writerow(
        [
            "Asha Verma",
            "asha.verma",
            "TempPass123",
            "EMP001",
            "Operations",
            "Executive",
            "+91-9000000001",
            "2026-01-15",
        ]
    )
    writer.writerow(
        [
            "Ravi Singh",
            "ravi.singh",
            "TempPass123",
            "EMP002",
            "HR",
            "Coordinator",
            "+91-9000000002",
            "2026-02-01",
        ]
    )

    payload = io.BytesIO(output.getvalue().encode("utf-8"))
    payload.seek(0)
    return send_file(
        payload,
        mimetype="text/csv",
        as_attachment=True,
        download_name="employee_import_template.csv",
    )


@app.route("/admin/employee/import", methods=["POST"])
@login_required
@admin_required
def import_employee_accounts():
    admin_user = current_user()
    quota = build_admin_quota_snapshot(admin_user)
    if not quota["can_create"]:
        flash("User creation limit reached. Contact main admin to increase your quota.", "danger")
        return redirect(url_for("dashboard"))

    upload = request.files.get("employee_csv")
    if not upload or not upload.filename:
        flash("Select a CSV file to import employees.", "danger")
        return redirect(url_for("dashboard"))
    if not upload.filename.strip().lower().endswith(".csv"):
        flash("Only CSV file uploads are supported.", "danger")
        return redirect(url_for("dashboard"))

    row_limit = 1000
    try:
        raw_bytes = upload.read()
        if len(raw_bytes) == 0:
            flash("Uploaded CSV is empty.", "danger")
            return redirect(url_for("dashboard"))
        csv_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("CSV must be UTF-8 encoded.", "danger")
        return redirect(url_for("dashboard"))

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        flash("CSV appears empty or header row is missing.", "danger")
        return redirect(url_for("dashboard"))

    required_fields = {"full_name", "username", "password"}
    field_alias = {}
    for field_name in reader.fieldnames:
        if field_name is None:
            continue
        normalized = field_name.strip().lower()
        if normalized:
            field_alias[normalized] = field_name

    missing_fields = sorted(required_fields - set(field_alias.keys()))
    if missing_fields:
        flash(f"Missing required CSV columns: {', '.join(missing_fields)}.", "danger")
        return redirect(url_for("dashboard"))

    raw_rows = list(reader)
    skipped_count = 0
    row_errors = []
    if len(raw_rows) > row_limit:
        extra_rows = len(raw_rows) - row_limit
        raw_rows = raw_rows[:row_limit]
        skipped_count += extra_rows
        row_errors.append(f"Import capped at {row_limit} rows; {extra_rows} extra rows ignored.")

    candidate_usernames = set()
    candidate_codes = set()

    def row_value(row_data: dict, key: str) -> str:
        source_key = field_alias.get(key)
        if not source_key:
            return ""
        return (row_data.get(source_key) or "").strip()

    for row in raw_rows:
        if not any((value or "").strip() for value in row.values()):
            continue
        username = _normalize_username(row_value(row, "username"), "")
        employee_code = row_value(row, "employee_code").upper()
        if username:
            candidate_usernames.add(username)
        if employee_code:
            candidate_codes.add(employee_code)

    existing_usernames = set()
    if candidate_usernames:
        existing_usernames = {
            row[0]
            for row in db.session.query(User.username)
            .filter(User.username.in_(list(candidate_usernames)))
            .all()
        }

    existing_codes = set()
    if candidate_codes:
        existing_codes = {
            row[0]
            for row in db.session.query(EmployeeProfile.employee_code)
            .filter(EmployeeProfile.employee_code.in_(list(candidate_codes)))
            .all()
            if row[0]
        }

    remaining_slots = max(0, int(quota["remaining"]))
    created_count = 0
    seen_usernames = set()
    seen_codes = set()

    def record_error(row_number: int, message: str):
        nonlocal skipped_count
        skipped_count += 1
        if len(row_errors) < 5:
            row_errors.append(f"Row {row_number}: {message}")

    for row_index, row in enumerate(raw_rows, start=2):
        if created_count >= remaining_slots:
            skipped_count += len(raw_rows) - (row_index - 2)
            row_errors.append("Quota reached during import. Remaining rows were skipped.")
            break

        if not any((value or "").strip() for value in row.values()):
            continue

        full_name = row_value(row, "full_name")
        username = _normalize_username(row_value(row, "username"), "")
        password = row_value(row, "password")
        employee_code = row_value(row, "employee_code").upper()
        department = row_value(row, "department")
        designation = row_value(row, "designation")
        phone = row_value(row, "phone")
        joining_date_raw = row_value(row, "joining_date")

        if not full_name or not username or not password:
            record_error(row_index, "full_name, username, and password are required.")
            continue
        if len(full_name) > 120:
            record_error(row_index, "full_name exceeds 120 characters.")
            continue
        if len(username) > 80:
            record_error(row_index, "username exceeds 80 characters.")
            continue
        if not _is_valid_username(username):
            record_error(row_index, "username has invalid characters.")
            continue
        if len(password) < 6:
            record_error(row_index, "password must be at least 6 characters.")
            continue
        if len(password) > 255:
            record_error(row_index, "password is too long.")
            continue
        if employee_code and len(employee_code) > 40:
            record_error(row_index, "employee_code exceeds 40 characters.")
            continue
        if department and len(department) > 80:
            record_error(row_index, "department exceeds 80 characters.")
            continue
        if designation and len(designation) > 80:
            record_error(row_index, "designation exceeds 80 characters.")
            continue
        if phone and len(phone) > 40:
            record_error(row_index, "phone exceeds 40 characters.")
            continue
        if username in seen_usernames:
            record_error(row_index, "duplicate username in this CSV.")
            continue
        if username in existing_usernames:
            record_error(row_index, "username already exists.")
            continue
        if employee_code:
            if employee_code in seen_codes:
                record_error(row_index, "duplicate employee_code in this CSV.")
                continue
            if employee_code in existing_codes:
                record_error(row_index, "employee_code already exists.")
                continue

        try:
            joining_date = parse_joining_date_or_none(joining_date_raw)
        except ValueError:
            record_error(row_index, "joining_date must be YYYY-MM-DD.")
            continue

        try:
            create_employee_with_profile(
                admin_user=admin_user,
                full_name=full_name,
                username=username,
                password=password,
                employee_code=employee_code,
                department=department,
                designation=designation,
                phone=phone,
                joining_date=joining_date,
            )
            db.session.commit()
            created_count += 1
            seen_usernames.add(username)
            existing_usernames.add(username)
            if employee_code:
                seen_codes.add(employee_code)
                existing_codes.add(employee_code)
        except IntegrityError:
            db.session.rollback()
            record_error(row_index, "username or employee_code already exists.")
        except SQLAlchemyError:
            db.session.rollback()
            record_error(row_index, "database write failed.")

    if created_count:
        flash(f"Bulk import completed. {created_count} employee account(s) created.", "success")
    if skipped_count or row_errors:
        summary = "; ".join(row_errors[:3])
        if len(row_errors) > 3:
            summary += " ..."
        message = f"Skipped {skipped_count} row(s)."
        if summary:
            message = f"{message} {summary}"
        flash(message, "warning")
    if not created_count and not skipped_count:
        flash("No data rows found in CSV.", "warning")

    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = current_user()
    if user.role == "super_admin":
        return redirect(url_for("super_admin_dashboard"))

    settings = get_settings()
    now_value = now_utc()
    start_day, end_day = local_day_range_as_utc(now_value)
    local_today = to_local_datetime(now_value).date()

    filters = {
        "event_type": (request.args.get("event_type") or "").strip().upper(),
        "source": (request.args.get("source") or "").strip().upper(),
        "date_from": (request.args.get("date_from") or "").strip(),
        "date_to": (request.args.get("date_to") or "").strip(),
        "user_id": (request.args.get("user_id") or "").strip(),
    }

    if user.role == "admin":
        users = (
            User.query.options(joinedload(User.profile))
            .filter(User.role == "user", User.company_admin_id == user.id)
            .order_by(User.full_name.asc())
            .all()
        )
        managed_user_ids = [item.id for item in users]
        events_by_user_today, _ = load_events_for_users_window(
            managed_user_ids, start_day, now_value
        )

        today_in_count = 0
        today_out_count = 0
        latest_events = []
        if managed_user_ids:
            today_in_count = (
                Attendance.query.join(User, Attendance.user_id == User.id)
                .filter(
                    User.role == "user",
                    User.company_admin_id == user.id,
                    Attendance.created_at >= start_day,
                    Attendance.created_at < end_day,
                    Attendance.event_type == "IN",
                )
                .count()
            )
            today_out_count = (
                Attendance.query.join(User, Attendance.user_id == User.id)
                .filter(
                    User.role == "user",
                    User.company_admin_id == user.id,
                    Attendance.created_at >= start_day,
                    Attendance.created_at < end_day,
                    Attendance.event_type == "OUT",
                )
                .count()
            )

            latest_events_subq = (
                db.session.query(
                    Attendance.user_id.label("user_id"),
                    db.func.max(Attendance.id).label("max_id"),
                )
                .join(User, Attendance.user_id == User.id)
                .filter(User.role == "user", User.company_admin_id == user.id)
                .group_by(Attendance.user_id)
                .subquery()
            )
            latest_events = (
                db.session.query(
                    Attendance.user_id,
                    Attendance.event_type,
                    Attendance.created_at,
                    User.full_name,
                )
                .join(latest_events_subq, Attendance.id == latest_events_subq.c.max_id)
                .join(User, User.id == Attendance.user_id)
                .filter(User.role == "user", User.company_admin_id == user.id)
                .all()
            )

        latest_event_map = {item.user_id: item for item in latest_events}
        active_inside_count = sum(1 for item in latest_events if item.event_type == "IN")
        live_inside = []
        for item in latest_events:
            if item.event_type != "IN":
                continue
            duration_seconds = int((now_value - item.created_at).total_seconds())
            live_inside.append(
                {
                    "name": item.full_name,
                    "since": format_dt(item.created_at),
                    "duration": format_duration(duration_seconds),
                }
            )

        avg_today_hours = 0.0
        late_today_count = 0
        overtime_today_hours = 0.0
        employee_rows = []

        for employee in users:
            profile = employee.profile or get_user_profile(employee, persist=False)
            summary = summarize_user_day_from_events(
                events_by_user_today.get(employee.id, []),
                local_today,
                now_value,
                settings,
            )
            avg_today_hours += summary["work_hours"]
            if summary["late_minutes"] > 0:
                late_today_count += 1
            overtime_today_hours += summary["overtime_hours"]

            latest_event = latest_event_map.get(employee.id)
            current_status = "IN" if latest_event and latest_event.event_type == "IN" else "OUT"

            employee_rows.append(
                {
                    "id": employee.id,
                    "full_name": employee.full_name,
                    "username": employee.username,
                    "is_active": employee.is_active,
                    "employee_code": profile.employee_code or "",
                    "department": profile.department or "",
                    "designation": profile.designation or "",
                    "phone": profile.phone or "",
                    "joining_date": profile.joining_date.isoformat() if profile.joining_date else "",
                    "today_hours": summary["work_hours_hhmm"],
                    "late_minutes": summary["late_minutes"],
                    "status": current_status,
                }
            )

        employee_count = max(1, len(users))
        avg_today_hours = round(avg_today_hours / employee_count, 2)

        stats = {
            "total_users": len(users),
            "today_in": today_in_count,
            "today_out": today_out_count,
            "active_inside": active_inside_count,
            "avg_today_hours": avg_today_hours,
            "late_today": late_today_count,
            "overtime_today_hours": round(overtime_today_hours, 2),
        }

        entries_query = (
            Attendance.query.options(joinedload(Attendance.submitted_by))
            .join(User, Attendance.user_id == User.id)
            .filter(User.role == "user", User.company_admin_id == user.id)
        )

        if filters["event_type"] in {"IN", "OUT"}:
            entries_query = entries_query.filter(Attendance.event_type == filters["event_type"])
        if filters["source"] in {"LIVE", "CORRECTION", "IMPORT"}:
            entries_query = entries_query.filter(Attendance.entry_source == filters["source"])
        if filters["user_id"].isdigit():
            requested_user_id = int(filters["user_id"])
            if requested_user_id in managed_user_ids:
                entries_query = entries_query.filter(Attendance.user_id == requested_user_id)
            else:
                flash("Employee filter is outside your account scope and was ignored.", "warning")

        if filters["date_from"]:
            try:
                from_day = datetime.strptime(filters["date_from"], "%Y-%m-%d").date()
                from_start, _ = local_date_range_as_utc(from_day)
                entries_query = entries_query.filter(Attendance.created_at >= from_start)
            except ValueError:
                flash("Invalid From date filter ignored.", "warning")

        if filters["date_to"]:
            try:
                to_day = datetime.strptime(filters["date_to"], "%Y-%m-%d").date()
                _, to_end = local_date_range_as_utc(to_day)
                entries_query = entries_query.filter(Attendance.created_at < to_end)
            except ValueError:
                flash("Invalid To date filter ignored.", "warning")

        entries = entries_query.order_by(Attendance.created_at.desc()).limit(800).all()

        pending_corrections = (
            AttendanceCorrection.query.options(
                joinedload(AttendanceCorrection.requester),
                joinedload(AttendanceCorrection.resolver),
            )
            .join(User, AttendanceCorrection.user_id == User.id)
            .filter(User.role == "user", User.company_admin_id == user.id)
            .filter(AttendanceCorrection.status == "PENDING")
            .order_by(AttendanceCorrection.created_at.desc())
            .limit(120)
            .all()
        )

        recent_corrections = (
            AttendanceCorrection.query.options(
                joinedload(AttendanceCorrection.requester),
                joinedload(AttendanceCorrection.resolver),
            )
            .join(User, AttendanceCorrection.user_id == User.id)
            .filter(User.role == "user", User.company_admin_id == user.id)
            .order_by(AttendanceCorrection.created_at.desc())
            .limit(200)
            .all()
        )
        admin_quota = build_admin_quota_snapshot(user)

        return render_template(
            "dashboard.html",
            user={
                "id": user.id,
                "full_name": user.full_name,
                "username": user.username,
                "role": user.role,
            },
            is_admin=True,
            stats=stats,
            records=[attendance_to_dict(item) for item in entries],
            settings_data={
                "company_name": settings.company_name,
                "shift_start": settings.shift_start,
                "shift_end": settings.shift_end,
                "grace_minutes": settings.grace_minutes,
                "full_day_hours": settings.full_day_hours,
                "geofence_lat": settings.geofence_lat,
                "geofence_lng": settings.geofence_lng,
                "geofence_radius_m": settings.geofence_radius_m,
                "geofence_enforced": settings.geofence_enforced,
            },
            employee_rows=employee_rows,
            all_users=[{"id": item.id, "full_name": item.full_name} for item in users],
            pending_corrections=[correction_to_dict(item) for item in pending_corrections],
            recent_corrections=[correction_to_dict(item) for item in recent_corrections],
            my_corrections=[],
            live_inside=live_inside,
            filters=filters,
            profile=None,
            admin_quota=admin_quota,
        )

    latest_event = get_latest_event_for_user(user.id)
    current_status = "IN" if latest_event and latest_event.event_type == "IN" else "OUT"
    summary_today = summarize_user_day(user.id, local_today, now_value, settings)

    entries = (
        Attendance.query.options(joinedload(Attendance.submitted_by))
        .filter_by(user_id=user.id)
        .order_by(Attendance.created_at.desc())
        .limit(200)
        .all()
    )

    user_corrections = (
        AttendanceCorrection.query.options(
            joinedload(AttendanceCorrection.requester),
            joinedload(AttendanceCorrection.resolver),
        )
        .filter(AttendanceCorrection.user_id == user.id)
        .order_by(AttendanceCorrection.created_at.desc())
        .limit(80)
        .all()
    )

    profile = user.profile or get_user_profile(user, persist=False)

    stats = {
        "current_status": current_status,
        "today_events": summary_today["events_count"],
        "today_work_hours": summary_today["work_hours_hhmm"],
        "first_in": summary_today["first_in"],
        "last_out": summary_today["last_out"],
        "late_by": summary_today["late_minutes"],
        "overtime_hours": summary_today["overtime_hours"],
    }

    return render_template(
        "dashboard.html",
        user={
            "id": user.id,
            "full_name": user.full_name,
            "username": user.username,
            "role": user.role,
        },
        is_admin=False,
        stats=stats,
        records=[attendance_to_dict(item) for item in entries],
        settings_data={
            "company_name": settings.company_name,
            "shift_start": settings.shift_start,
            "shift_end": settings.shift_end,
            "grace_minutes": settings.grace_minutes,
            "full_day_hours": settings.full_day_hours,
            "geofence_lat": settings.geofence_lat,
            "geofence_lng": settings.geofence_lng,
            "geofence_radius_m": settings.geofence_radius_m,
            "geofence_enforced": settings.geofence_enforced,
        },
        employee_rows=[],
        all_users=[],
        pending_corrections=[],
        recent_corrections=[],
        my_corrections=[correction_to_dict(item) for item in user_corrections],
        live_inside=[],
        filters=filters,
        admin_quota=None,
        profile={
            "employee_code": profile.employee_code or "",
            "department": profile.department or "",
            "designation": profile.designation or "",
            "phone": profile.phone or "",
            "joining_date": profile.joining_date.isoformat() if profile.joining_date else "",
        },
    )

@app.route("/attendance", methods=["POST"])
@login_required
def submit_attendance():
    payload = request.get_json(silent=True) or {}
    person_name = payload.get("person_name", "").strip()
    image_data = (payload.get("image_data") or "").strip()
    location_text = payload.get("location_text", "").strip()
    action_type = normalize_event_type(payload.get("action_type", ""))
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")

    if not person_name:
        return jsonify({"ok": False, "error": "Name is required."}), 400
    if len(person_name) > 120:
        return jsonify({"ok": False, "error": "Name is too long."}), 400
    if not action_type:
        return jsonify({"ok": False, "error": "Action type must be IN or OUT."}), 400
    if latitude is None or longitude is None:
        return jsonify({"ok": False, "error": "Location is required."}), 400

    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid location values."}), 400

    if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
        return jsonify({"ok": False, "error": "Location coordinates are out of range."}), 400
    if len(location_text) > 300:
        location_text = location_text[:300]

    normalized_image_data = None
    if image_data:
        try:
            normalized_image_data = _validate_and_normalize_image(image_data)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    user = current_user()
    if not user:
        session.clear()
        return jsonify({"ok": False, "error": "Session expired. Please login again."}), 401
    if not user.is_active:
        session.clear()
        return jsonify({"ok": False, "error": "Account is inactive."}), 403

    settings = get_settings()
    geofence_ok, geofence_error, distance_m = evaluate_geofence(latitude, longitude, settings)
    if not geofence_ok:
        return jsonify({"ok": False, "error": geofence_error}), 403

    now_value = now_utc()
    latest_event = get_latest_event_for_user(user.id)

    if latest_event:
        elapsed = (now_value - latest_event.created_at).total_seconds()
        if latest_event.event_type == action_type and elapsed < 30:
            return jsonify(
                {
                    "ok": False,
                    "error": "Duplicate punch detected. Wait a few seconds and retry.",
                }
            ), 429

    if action_type == "IN" and latest_event and latest_event.event_type == "IN":
        return jsonify(
            {
                "ok": False,
                "error": "You are already marked IN. Mark OUT before IN again.",
            }
        ), 409

    if action_type == "OUT" and (not latest_event or latest_event.event_type != "IN"):
        return jsonify(
            {
                "ok": False,
                "error": "You must mark IN before marking OUT.",
            }
        ), 409

    effective_location_text = location_text or f"{latitude:.6f}, {longitude:.6f}"
    if distance_m is not None and settings.geofence_lat is not None and settings.geofence_lng is not None:
        effective_location_text = (
            f"{effective_location_text} (Distance {int(distance_m)}m from office)"
        )
    if len(effective_location_text) > 300:
        effective_location_text = effective_location_text[:300]

    entry = Attendance(
        user_id=user.id,
        person_name=person_name,
        photo_data=normalized_image_data,
        photo_path="db-inline" if normalized_image_data else "",
        event_type=action_type,
        entry_source="LIVE",
        latitude=latitude,
        longitude=longitude,
        location_text=effective_location_text,
        created_at=now_value,
    )

    try:
        db.session.add(entry)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return jsonify({"ok": False, "error": "Unable to save attendance right now."}), 500

    return jsonify(
        {
            "ok": True,
            "message": f"{action_type} event submitted.",
            "created_at": format_dt(now_value),
            "event_type": action_type,
            "distance_m": int(distance_m) if distance_m is not None else None,
        }
    )


@app.route("/correction/request", methods=["POST"])
@login_required
def request_correction():
    user = current_user()

    request_type = (request.form.get("request_type") or "").strip().upper()
    proposed_event_type = normalize_event_type(request.form.get("proposed_event_type", ""))
    requested_datetime = parse_local_datetime_input(request.form.get("requested_datetime", ""))
    reason = (request.form.get("reason") or "").strip()

    if request_type not in ALLOWED_CORRECTION_TYPES:
        flash("Invalid correction type.", "danger")
        return redirect(url_for("dashboard"))

    if request_type == "MISSING_IN":
        proposed_event_type = "IN"
    elif request_type == "MISSING_OUT":
        proposed_event_type = "OUT"

    if not proposed_event_type:
        flash("Select a valid event type for correction.", "danger")
        return redirect(url_for("dashboard"))

    if not requested_datetime:
        flash("Requested date/time is invalid.", "danger")
        return redirect(url_for("dashboard"))
    if requested_datetime > now_utc() + timedelta(minutes=1):
        flash("Requested date/time cannot be in the future.", "danger")
        return redirect(url_for("dashboard"))

    if len(reason) < 8:
        flash("Reason must be at least 8 characters.", "danger")
        return redirect(url_for("dashboard"))
    if len(reason) > 800:
        flash("Reason is too long.", "danger")
        return redirect(url_for("dashboard"))

    correction = AttendanceCorrection(
        user_id=user.id,
        request_type=request_type,
        proposed_event_type=proposed_event_type,
        requested_datetime=requested_datetime,
        reason=reason,
        status="PENDING",
        created_at=now_utc(),
    )

    try:
        db.session.add(correction)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("Could not submit correction request right now.", "danger")
        return redirect(url_for("dashboard"))

    flash("Correction request submitted for admin review.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/corrections/<int:correction_id>/resolve", methods=["POST"])
@admin_required
def resolve_correction(correction_id: int):
    correction = db.session.get(AttendanceCorrection, correction_id)
    if not correction:
        flash("Correction request not found.", "warning")
        return redirect(url_for("dashboard"))

    if correction.status != "PENDING":
        flash("Correction request already resolved.", "warning")
        return redirect(url_for("dashboard"))

    decision = (request.form.get("decision") or "").strip().upper()
    admin_note = (request.form.get("admin_note") or "").strip()
    admin_user = current_user()
    requester = correction.requester

    if (
        not requester
        or requester.role != "user"
        or requester.company_admin_id != admin_user.id
    ):
        flash("You are not authorized to resolve this correction request.", "danger")
        return redirect(url_for("dashboard"))

    if decision not in {"APPROVE", "REJECT"}:
        flash("Invalid decision.", "danger")
        return redirect(url_for("dashboard"))

    correction.status = "APPROVED" if decision == "APPROVE" else "REJECTED"
    correction.admin_note = admin_note[:800] if admin_note else None
    correction.resolved_at = now_utc()
    correction.resolved_by_id = admin_user.id

    if decision == "APPROVE":
        manual_entry = Attendance(
            user_id=requester.id,
            person_name=requester.full_name,
            photo_data=None,
            photo_path="",
            event_type=correction.proposed_event_type,
            entry_source="CORRECTION",
            latitude=0.0,
            longitude=0.0,
            location_text="Manual correction approved by admin",
            created_at=correction.requested_datetime,
        )
        db.session.add(manual_entry)

    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("Unable to update correction request right now.", "danger")
        return redirect(url_for("dashboard"))

    flash("Correction request updated successfully.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/settings", methods=["POST"])
@admin_required
def update_settings():
    settings = get_settings()

    company_name = (request.form.get("company_name") or "").strip()
    shift_start = (request.form.get("shift_start") or "").strip()
    shift_end = (request.form.get("shift_end") or "").strip()
    grace_minutes_raw = (request.form.get("grace_minutes") or "").strip()
    full_day_hours_raw = (request.form.get("full_day_hours") or "").strip()
    geofence_lat_raw = (request.form.get("geofence_lat") or "").strip()
    geofence_lng_raw = (request.form.get("geofence_lng") or "").strip()
    geofence_radius_raw = (request.form.get("geofence_radius_m") or "").strip()
    geofence_enforced = request.form.get("geofence_enforced") == "on"

    if not company_name:
        flash("Company name is required.", "danger")
        return redirect(url_for("dashboard"))
    if len(company_name) > 120:
        flash("Company name is too long.", "danger")
        return redirect(url_for("dashboard"))

    if not is_valid_hhmm(shift_start) or not is_valid_hhmm(shift_end):
        flash("Shift start/end must be in HH:MM format.", "danger")
        return redirect(url_for("dashboard"))

    try:
        grace_minutes = int(grace_minutes_raw)
        if grace_minutes < 0 or grace_minutes > 240:
            raise ValueError
    except ValueError:
        flash("Grace minutes must be between 0 and 240.", "danger")
        return redirect(url_for("dashboard"))

    try:
        full_day_hours = float(full_day_hours_raw)
        if full_day_hours < 1 or full_day_hours > 16:
            raise ValueError
    except ValueError:
        flash("Full day hours must be between 1 and 16.", "danger")
        return redirect(url_for("dashboard"))

    geofence_lat = None
    geofence_lng = None
    if geofence_lat_raw or geofence_lng_raw:
        try:
            geofence_lat = float(geofence_lat_raw)
            geofence_lng = float(geofence_lng_raw)
            if not (-90.0 <= geofence_lat <= 90.0) or not (-180.0 <= geofence_lng <= 180.0):
                raise ValueError
        except ValueError:
            flash("Geofence coordinates are invalid.", "danger")
            return redirect(url_for("dashboard"))

    try:
        geofence_radius_m = int(geofence_radius_raw or "300")
        if geofence_radius_m < 20 or geofence_radius_m > 50000:
            raise ValueError
    except ValueError:
        flash("Geofence radius must be between 20 and 50000 meters.", "danger")
        return redirect(url_for("dashboard"))

    settings.company_name = company_name
    settings.shift_start = shift_start
    settings.shift_end = shift_end
    settings.grace_minutes = grace_minutes
    settings.full_day_hours = full_day_hours
    settings.geofence_lat = geofence_lat
    settings.geofence_lng = geofence_lng
    settings.geofence_radius_m = geofence_radius_m
    settings.geofence_enforced = geofence_enforced
    settings.updated_at = now_utc()

    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("Unable to save settings right now.", "danger")
        return redirect(url_for("dashboard"))

    flash("Organization settings updated.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/employee/<int:user_id>/profile", methods=["POST"])
@admin_required
def update_employee_profile(user_id: int):
    admin_user = current_user()
    employee = db.session.get(User, user_id)
    if (
        not employee
        or employee.role != "user"
        or employee.company_admin_id != admin_user.id
    ):
        flash("Employee not found.", "warning")
        return redirect(url_for("dashboard"))

    full_name = (request.form.get("full_name") or "").strip()
    employee_code = (request.form.get("employee_code") or "").strip().upper()
    department = (request.form.get("department") or "").strip()
    designation = (request.form.get("designation") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    new_password = request.form.get("new_password", "")
    joining_date_raw = (request.form.get("joining_date") or "").strip()
    is_active = request.form.get("is_active") == "on"

    if not full_name:
        flash("Employee name is required.", "danger")
        return redirect(url_for("dashboard"))
    if len(full_name) > 120:
        flash("Employee name is too long.", "danger")
        return redirect(url_for("dashboard"))
    if new_password and len(new_password) < 6:
        flash("If provided, new password must be at least 6 characters.", "danger")
        return redirect(url_for("dashboard"))
    if new_password and len(new_password) > 255:
        flash("If provided, new password is too long.", "danger")
        return redirect(url_for("dashboard"))
    if employee_code and len(employee_code) > 40:
        flash("Employee code is too long.", "danger")
        return redirect(url_for("dashboard"))
    if department and len(department) > 80:
        flash("Department is too long.", "danger")
        return redirect(url_for("dashboard"))
    if designation and len(designation) > 80:
        flash("Designation is too long.", "danger")
        return redirect(url_for("dashboard"))
    if phone and len(phone) > 40:
        flash("Phone is too long.", "danger")
        return redirect(url_for("dashboard"))

    try:
        joining_date = parse_joining_date_or_none(joining_date_raw)
    except ValueError:
        flash("Joining date format is invalid.", "danger")
        return redirect(url_for("dashboard"))

    profile = get_user_profile(employee)
    employee.full_name = full_name
    employee.is_active = is_active
    if new_password:
        employee.password_hash = generate_password_hash(new_password)

    profile.employee_code = employee_code or None
    profile.department = department or None
    profile.designation = designation or None
    profile.phone = phone or None
    profile.joining_date = joining_date
    profile.updated_at = now_utc()

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("Employee code must be unique.", "danger")
        return redirect(url_for("dashboard"))
    except SQLAlchemyError:
        db.session.rollback()
        flash("Could not update employee profile right now.", "danger")
        return redirect(url_for("dashboard"))

    if new_password:
        flash("Employee profile updated and password reset.", "success")
    else:
        flash("Employee profile updated.", "success")
    return redirect(url_for("dashboard"))


@app.route("/admin/attendance/<int:attendance_id>/delete", methods=["POST"])
@admin_required
def delete_attendance(attendance_id: int):
    admin_user = current_user()
    entry = db.session.get(Attendance, attendance_id)
    submitted_by = entry.submitted_by if entry else None
    if (
        not entry
        or not submitted_by
        or submitted_by.role != "user"
        or submitted_by.company_admin_id != admin_user.id
    ):
        flash("Attendance record not found.", "warning")
        return redirect(url_for("dashboard"))

    try:
        db.session.delete(entry)
        db.session.commit()
        flash("Attendance record deleted.", "success")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Unable to delete attendance right now. Please retry.", "danger")

    return redirect(url_for("dashboard"))


@app.route("/admin/analytics-page")
@admin_required
def admin_analytics_page():
    admin_user = current_user()
    analytics_years_limit = clamp_analytics_years(admin_user.analytics_years_limit, 1)
    return render_template(
        "admin_analytics.html",
        analytics_years_limit=analytics_years_limit,
    )


@app.route("/admin/analytics")
@admin_required
def admin_analytics():
    admin_user = current_user()
    allowed_years = clamp_analytics_years(admin_user.analytics_years_limit, 1)
    years_raw = (request.args.get("years") or "").strip()
    try:
        requested_years = int(years_raw) if years_raw else 1
    except ValueError:
        requested_years = 1
    requested_years = clamp_analytics_years(requested_years, 1)
    effective_years = min(allowed_years, requested_years)

    payload = build_admin_analytics_payload(
        now_utc(),
        company_admin_id=admin_user.id,
        years=effective_years,
    )
    payload["allowed_years"] = allowed_years
    payload["requested_years"] = effective_years
    payload["requested_years_limited"] = requested_years > effective_years
    return jsonify({"ok": True, **payload})


@app.route("/admin/analytics/import-template")
@admin_required
def download_analytics_import_template():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "username",
            "person_name",
            "event_type",
            "created_at",
            "latitude",
            "longitude",
            "location_text",
        ]
    )
    writer.writerow(
        [
            "emp1",
            "Ravi Kumar",
            "IN",
            "2025-01-05 09:31:00",
            "28.6139",
            "77.2090",
            "Delhi Office Gate A",
        ]
    )
    writer.writerow(
        [
            "emp1",
            "Ravi Kumar",
            "OUT",
            "2025-01-05 18:42:00",
            "28.6139",
            "77.2090",
            "Delhi Office Gate A",
        ]
    )
    writer.writerow(
        [
            "emp2",
            "Anita Singh",
            "IN",
            "2025-01-05T09:20:00",
            "28.6139",
            "77.2090",
            "Delhi Office",
        ]
    )

    payload = io.BytesIO(output.getvalue().encode("utf-8"))
    payload.seek(0)
    return send_file(
        payload,
        mimetype="text/csv",
        as_attachment=True,
        download_name="analytics_attendance_import_template.csv",
    )


@app.route("/admin/analytics/import", methods=["POST"])
@admin_required
def import_analytics_csv():
    admin_user = current_user()
    upload = request.files.get("analytics_csv")
    if not upload or not upload.filename:
        flash("Select a CSV file to import analytics attendance data.", "danger")
        return redirect(url_for("admin_analytics_page"))
    if not upload.filename.strip().lower().endswith(".csv"):
        flash("Only CSV file uploads are supported.", "danger")
        return redirect(url_for("admin_analytics_page"))

    try:
        raw_bytes = upload.read()
        if len(raw_bytes) == 0:
            flash("Uploaded CSV is empty.", "danger")
            return redirect(url_for("admin_analytics_page"))
        csv_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("CSV must be UTF-8 encoded.", "danger")
        return redirect(url_for("admin_analytics_page"))

    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        flash("CSV appears empty or header row is missing.", "danger")
        return redirect(url_for("admin_analytics_page"))

    required_fields = {"username", "event_type", "created_at"}
    field_alias = {}
    for field_name in reader.fieldnames:
        if field_name is None:
            continue
        normalized = field_name.strip().lower()
        if normalized:
            field_alias[normalized] = field_name

    missing_fields = sorted(required_fields - set(field_alias.keys()))
    if missing_fields:
        flash(f"Missing required CSV columns: {', '.join(missing_fields)}.", "danger")
        return redirect(url_for("admin_analytics_page"))

    raw_rows = list(reader)
    row_limit = max(500, _env_int("ANALYTICS_IMPORT_ROW_LIMIT", 20000))
    skipped_count = 0
    row_errors = []
    if len(raw_rows) > row_limit:
        extra_rows = len(raw_rows) - row_limit
        raw_rows = raw_rows[:row_limit]
        skipped_count += extra_rows
        row_errors.append(f"Import capped at {row_limit} rows; {extra_rows} extra rows ignored.")

    managed_users = (
        User.query.options(joinedload(User.profile))
        .filter(User.role == "user", User.company_admin_id == admin_user.id)
        .all()
    )
    username_map = {item.username: item for item in managed_users}

    def row_value(row_data: dict, key: str) -> str:
        source_key = field_alias.get(key)
        if not source_key:
            return ""
        return (row_data.get(source_key) or "").strip()

    def record_error(row_number: int, message: str):
        nonlocal skipped_count
        skipped_count += 1
        if len(row_errors) < 6:
            row_errors.append(f"Row {row_number}: {message}")

    now_value = now_utc()
    candidates = []
    for row_index, row in enumerate(raw_rows, start=2):
        if not any((value or "").strip() for value in row.values()):
            continue

        username = _normalize_username(row_value(row, "username"), "")
        user = username_map.get(username)
        if not user:
            record_error(row_index, "username not found in your company users.")
            continue

        event_type = normalize_event_type(row_value(row, "event_type"))
        if not event_type:
            record_error(row_index, "event_type must be IN or OUT.")
            continue

        created_at = parse_import_datetime_input(row_value(row, "created_at"))
        if not created_at:
            record_error(row_index, "created_at is invalid.")
            continue
        if created_at > now_value + timedelta(minutes=1):
            record_error(row_index, "created_at cannot be in the future.")
            continue

        person_name = (row_value(row, "person_name") or user.full_name)[:120]
        if not person_name:
            record_error(row_index, "person_name is required.")
            continue

        latitude = 0.0
        longitude = 0.0
        latitude_raw = row_value(row, "latitude")
        longitude_raw = row_value(row, "longitude")
        if latitude_raw:
            try:
                latitude = float(latitude_raw)
            except ValueError:
                record_error(row_index, "latitude is invalid.")
                continue
        if longitude_raw:
            try:
                longitude = float(longitude_raw)
            except ValueError:
                record_error(row_index, "longitude is invalid.")
                continue
        if not (-90.0 <= latitude <= 90.0):
            record_error(row_index, "latitude is out of range.")
            continue
        if not (-180.0 <= longitude <= 180.0):
            record_error(row_index, "longitude is out of range.")
            continue

        location_text = row_value(row, "location_text")[:300]
        candidates.append(
            {
                "user_id": user.id,
                "person_name": person_name,
                "event_type": event_type,
                "created_at": created_at,
                "latitude": latitude,
                "longitude": longitude,
                "location_text": location_text,
            }
        )

    created_count = 0
    if candidates:
        user_ids = sorted({item["user_id"] for item in candidates})
        min_created_at = min(item["created_at"] for item in candidates)
        max_created_at = max(item["created_at"] for item in candidates)
        existing_rows = (
            db.session.query(Attendance.user_id, Attendance.event_type, Attendance.created_at)
            .filter(
                Attendance.user_id.in_(user_ids),
                Attendance.created_at >= min_created_at,
                Attendance.created_at <= max_created_at,
            )
            .all()
        )
        existing_keys = {
            (row[0], row[1], row[2].replace(microsecond=0))
            for row in existing_rows
        }
        seen_new_keys = set()
        insert_rows = []
        for item in candidates:
            key = (item["user_id"], item["event_type"], item["created_at"].replace(microsecond=0))
            if key in existing_keys or key in seen_new_keys:
                skipped_count += 1
                continue
            insert_rows.append(
                Attendance(
                    user_id=item["user_id"],
                    person_name=item["person_name"],
                    photo_data=None,
                    photo_path="",
                    event_type=item["event_type"],
                    entry_source="IMPORT",
                    latitude=item["latitude"],
                    longitude=item["longitude"],
                    location_text=item["location_text"] or "Imported from analytics CSV",
                    created_at=item["created_at"],
                )
            )
            seen_new_keys.add(key)

        if insert_rows:
            try:
                db.session.add_all(insert_rows)
                db.session.commit()
                created_count = len(insert_rows)
            except SQLAlchemyError:
                db.session.rollback()
                flash("Unable to import analytics attendance data right now.", "danger")
                return redirect(url_for("admin_analytics_page"))

    if created_count:
        flash(f"Analytics import completed. {created_count} attendance row(s) added.", "success")
    if skipped_count or row_errors:
        summary = "; ".join(row_errors[:3])
        if len(row_errors) > 3:
            summary += " ..."
        message = f"Skipped {skipped_count} row(s)."
        if summary:
            message = f"{message} {summary}"
        flash(message, "warning")
    if not created_count and not skipped_count:
        flash("No data rows found in CSV.", "warning")

    return redirect(url_for("admin_analytics_page"))


@app.route("/admin/export")
@admin_required
def export_attendance():
    admin_user = current_user()
    event_type = (request.args.get("event_type") or "").strip().upper()
    source = (request.args.get("source") or "").strip().upper()
    user_id = (request.args.get("user_id") or "").strip()
    start_date_raw = (request.args.get("start_date") or "").strip()
    end_date_raw = (request.args.get("end_date") or "").strip()

    query = (
        Attendance.query.options(joinedload(Attendance.submitted_by))
        .join(User, Attendance.user_id == User.id)
        .filter(User.role == "user", User.company_admin_id == admin_user.id)
    )

    if event_type in {"IN", "OUT"}:
        query = query.filter(Attendance.event_type == event_type)
    if source in {"LIVE", "CORRECTION", "IMPORT"}:
        query = query.filter(Attendance.entry_source == source)
    if user_id.isdigit():
        requested_user_id = int(user_id)
        is_managed_user = (
            db.session.query(User.id)
            .filter(
                User.id == requested_user_id,
                User.role == "user",
                User.company_admin_id == admin_user.id,
            )
            .first()
            is not None
        )
        if is_managed_user:
            query = query.filter(Attendance.user_id == requested_user_id)
        else:
            flash("Requested employee is outside your account scope.", "warning")

    if start_date_raw:
        try:
            start_day = datetime.strptime(start_date_raw, "%Y-%m-%d").date()
            start_utc, _ = local_date_range_as_utc(start_day)
            query = query.filter(Attendance.created_at >= start_utc)
        except ValueError:
            flash("Invalid export start date ignored.", "warning")

    if end_date_raw:
        try:
            end_day = datetime.strptime(end_date_raw, "%Y-%m-%d").date()
            _, end_utc = local_date_range_as_utc(end_day)
            query = query.filter(Attendance.created_at < end_utc)
        except ValueError:
            flash("Invalid export end date ignored.", "warning")

    filename = f"attendance_export_{date.today().isoformat()}.csv"

    def csv_row_stream():
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        writer.writerow(
            [
                "Attendance ID",
                "Person Name",
                "Event Type",
                "Entry Source",
                "Submitted By",
                "Username",
                "Latitude",
                "Longitude",
                "Location Text",
                "Timestamp (Local)",
            ]
        )
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

        for row in query.order_by(Attendance.created_at.desc()).yield_per(1000):
            submitted_by = row.submitted_by
            submitted_by_name = submitted_by.full_name if submitted_by else "Deleted User"
            submitted_by_username = submitted_by.username if submitted_by else "deleted"
            writer.writerow(
                [
                    row.id,
                    row.person_name,
                    row.event_type,
                    row.entry_source,
                    submitted_by_name,
                    submitted_by_username,
                    row.latitude,
                    row.longitude,
                    row.location_text or "",
                    format_dt(row.created_at),
                ]
            )
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    response = Response(stream_with_context(csv_row_stream()), mimetype="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ── Startup diagnostic: log the resolved DATABASE_URL (password masked) ──
_startup_url = _database_url()
_masked = _startup_url
if "@" in _startup_url:
    _pre, _post = _startup_url.split("@", 1)
    _scheme_user = _pre.rsplit(":", 1)[0]  # scheme://user
    _masked = f"{_scheme_user}:***@{_post}"
app.logger.info("DATABASE_URL resolved to: %s", _masked)
del _startup_url, _masked

try:
    init_db()
except Exception as startup_exc:
    STARTUP_ERROR_MESSAGE = str(startup_exc)
    app.logger.exception(
        "Startup initialization failed. Service is running in degraded mode until fixed."
    )


@app.route("/health")
def health_check():
    """Diagnostic endpoint — no auth required."""
    info = {"status": "ok", "startup_error": STARTUP_ERROR_MESSAGE or None}
    url = _database_url()
    if "@" in url:
        pre, post = url.split("@", 1)
        scheme_user = pre.rsplit(":", 1)[0]
        info["db_url"] = f"{scheme_user}:***@{post}"
    else:
        info["db_url"] = url[:60]
    try:
        db.session.execute(text("SELECT 1"))
        info["db_reachable"] = True
    except Exception as exc:
        info["db_reachable"] = False
        info["db_error"] = str(exc)[:300]
    if STARTUP_ERROR_MESSAGE:
        info["status"] = "degraded"
    return info, 200 if info["status"] == "ok" else 503


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=max(1, _env_int("PORT", 5000)),
        debug=_env_bool("FLASK_DEBUG", False),
    )
