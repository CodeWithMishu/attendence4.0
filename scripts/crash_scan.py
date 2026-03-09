import io
import os
import re
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _build_app():
    fd, db_path = tempfile.mkstemp(prefix="attendance-crash-scan-", suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    os.environ["DATABASE_URL"] = "sqlite:///" + db_path.replace("\\", "/")
    os.environ["FLASK_ENV"] = "development"

    import app as app_module

    return app_module, db_path


def run_crash_scan():
    app_module, db_path = _build_app()
    app = app_module.app
    user_model = app_module.User
    attendance_model = app_module.Attendance
    client = app.test_client()

    errors: list[tuple[str, object, str]] = []

    def get_csrf(path="/login"):
        response = client.get(path, follow_redirects=True)
        if response.status_code >= 500:
            errors.append((f"GET {path} (csrf seed)", response.status_code, response.get_data(as_text=True)[:240]))
        with client.session_transaction() as session:
            return session.get("_csrf_token")

    def login(username: str, password: str):
        token = get_csrf("/login")
        response = client.post(
            "/login",
            data={"username": username, "password": password, "_csrf_token": token},
            follow_redirects=False,
        )
        if response.status_code not in (302, 303):
            errors.append((f"LOGIN {username}", response.status_code, response.get_data(as_text=True)[:240]))
            return False
        return True

    def logout():
        client.get("/logout", follow_redirects=False)

    try:
        assert login("superadmin", "Admin@123")
        token = get_csrf("/super-admin")
        client.post(
            "/super-admin/company-admin/create",
            data={
                "full_name": "Scan Admin",
                "username": "scanadmin",
                "password": "ScanAdmin@123",
                "user_limit": "10",
                "analytics_years_limit": "2",
                "_csrf_token": token,
            },
            follow_redirects=True,
        )
        logout()

        assert login("scanadmin", "ScanAdmin@123")
        token = get_csrf("/dashboard")
        client.post(
            "/admin/employee/create",
            data={
                "full_name": "Scan User",
                "username": "scanuser",
                "password": "ScanUser@123",
                "_csrf_token": token,
            },
            follow_redirects=True,
        )
        token = get_csrf("/dashboard")
        client.post(
            "/attendance",
            json={
                "person_name": "Scan User",
                "action_type": "IN",
                "latitude": 28.61,
                "longitude": 77.20,
                "location_text": "HQ",
            },
            headers={"X-CSRF-Token": token},
        )
        token = get_csrf("/dashboard")
        client.post(
            "/attendance",
            json={
                "person_name": "Scan User",
                "action_type": "OUT",
                "latitude": 28.61,
                "longitude": 77.20,
                "location_text": "HQ",
            },
            headers={"X-CSRF-Token": token},
        )
        logout()

        with app.app_context():
            admin_id = user_model.query.filter_by(username="scanadmin").first().id
            user_id = user_model.query.filter_by(username="scanuser").first().id
            first_attendance = attendance_model.query.first()
            attendance_id = first_attendance.id if first_attendance else 1

        id_map = {
            "admin_id": admin_id,
            "user_id": user_id,
            "attendance_id": attendance_id,
            "correction_id": 1,
        }

        def build_path(rule_text: str) -> str:
            def replace_typed(match: re.Match):
                converter = match.group(1)
                name = match.group(2)
                if converter in {"int", "float"}:
                    return str(id_map.get(name, 1))
                return "x"

            path = re.sub(r"<(int|float|string|path):([^>]+)>", replace_typed, rule_text)
            path = re.sub(r"<([^>]+)>", "1", path)
            return path

        roles = [
            ("anon", None, None),
            ("super_admin", "superadmin", "Admin@123"),
            ("admin", "scanadmin", "ScanAdmin@123"),
            ("user", "scanuser", "ScanUser@123"),
        ]

        for role_name, username, password in roles:
            logout()
            if username:
                login(username, password)

            for rule in sorted(app.url_map.iter_rules(), key=lambda item: item.rule):
                if rule.endpoint == "static":
                    continue
                path = build_path(rule.rule)

                if "GET" in rule.methods:
                    try:
                        response = client.get(path, follow_redirects=False)
                        if response.status_code >= 500:
                            errors.append(
                                (
                                    f"{role_name} GET {path}",
                                    response.status_code,
                                    response.get_data(as_text=True)[:240],
                                )
                            )
                    except Exception as exc:  # pragma: no cover - defensive scan
                        errors.append((f"{role_name} GET {path}", "EXC", repr(exc)))

                if "POST" in rule.methods:
                    try:
                        token = get_csrf("/dashboard" if username else "/login")
                        if path == "/attendance":
                            response = client.post(
                                path,
                                json={},
                                headers={"X-CSRF-Token": token},
                                follow_redirects=False,
                            )
                        elif "analytics/import" in path:
                            response = client.post(
                                path,
                                data={
                                    "_csrf_token": token,
                                    "analytics_csv": (io.BytesIO(b""), "x.csv"),
                                },
                                content_type="multipart/form-data",
                                follow_redirects=False,
                            )
                        else:
                            response = client.post(
                                path, data={"_csrf_token": token}, follow_redirects=False
                            )
                        if response.status_code >= 500:
                            errors.append(
                                (
                                    f"{role_name} POST {path}",
                                    response.status_code,
                                    response.get_data(as_text=True)[:240],
                                )
                            )
                    except Exception as exc:  # pragma: no cover - defensive scan
                        errors.append((f"{role_name} POST {path}", "EXC", repr(exc)))
    finally:
        try:
            if os.path.exists(db_path):
                os.remove(db_path)
        except OSError:
            pass

    if errors:
        for item in errors:
            print(item)
        raise SystemExit(1)


if __name__ == "__main__":
    run_crash_scan()
    print("CRASH_SCAN_OK")
