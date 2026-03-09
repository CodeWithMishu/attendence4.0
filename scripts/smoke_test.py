import io
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _build_app():
    fd, db_path = tempfile.mkstemp(prefix="attendance-smoke-", suffix=".db")
    os.close(fd)
    os.unlink(db_path)
    os.environ["DATABASE_URL"] = "sqlite:///" + db_path.replace("\\", "/")
    os.environ["FLASK_ENV"] = "development"

    import app as app_module

    return app_module, db_path


def run_smoke_test():
    app_module, db_path = _build_app()
    app = app_module.app
    user_model = app_module.User
    attendance_model = app_module.Attendance

    client = app.test_client()

    def get_csrf(path="/login"):
        response = client.get(path, follow_redirects=True)
        assert response.status_code == 200, f"GET {path} failed: {response.status_code}"
        with client.session_transaction() as session:
            token = session.get("_csrf_token")
        assert token, f"CSRF token missing after GET {path}"
        return token

    try:
        response = client.get("/healthz")
        assert response.status_code == 200 and response.json.get("ok") is True
        response = client.get("/readyz")
        assert response.status_code == 200 and response.json.get("ok") is True

        csrf = get_csrf("/login")
        response = client.post(
            "/login",
            data={"username": "superadmin", "password": "Admin@123", "_csrf_token": csrf},
            follow_redirects=False,
        )
        assert response.status_code in (302, 303)

        response = client.get("/super-admin")
        assert response.status_code == 200

        csrf = get_csrf("/super-admin")
        response = client.post(
            "/super-admin/company-admin/create",
            data={
                "full_name": "Company Admin One",
                "username": "adminone",
                "password": "AdminOne@123",
                "user_limit": "5",
                "analytics_years_limit": "3",
                "_csrf_token": csrf,
            },
            follow_redirects=True,
        )
        assert response.status_code == 200

        with app.app_context():
            admin_one = user_model.query.filter_by(username="adminone").first()
            assert admin_one is not None and admin_one.role == "admin"
            admin_one_id = admin_one.id

        csrf = get_csrf("/super-admin")
        response = client.post(
            f"/super-admin/company-admin/{admin_one_id}/analytics-limit",
            data={"analytics_years_limit": "4", "_csrf_token": csrf},
            follow_redirects=True,
        )
        assert response.status_code == 200

        response = client.get("/logout", follow_redirects=False)
        assert response.status_code in (302, 303)

        csrf = get_csrf("/login")
        response = client.post(
            "/login",
            data={"username": "adminone", "password": "AdminOne@123", "_csrf_token": csrf},
            follow_redirects=False,
        )
        assert response.status_code in (302, 303)

        response = client.get("/dashboard")
        assert response.status_code == 200

        csrf = get_csrf("/dashboard")
        response = client.post(
            "/admin/employee/create",
            data={
                "full_name": "Employee One",
                "username": "empone",
                "password": "EmpOne@123",
                "employee_code": "E001",
                "department": "Ops",
                "designation": "Associate",
                "phone": "9999999999",
                "joining_date": "2025-01-01",
                "_csrf_token": csrf,
            },
            follow_redirects=True,
        )
        assert response.status_code == 200

        response = client.get("/admin/analytics?years=10")
        assert response.status_code == 200
        payload = response.get_json(force=True)
        assert payload.get("ok") is True
        assert payload.get("allowed_years") == 4
        assert payload.get("requested_years") == 4
        assert payload.get("requested_years_limited") is True

        response = client.get("/admin/analytics/import-template")
        assert response.status_code == 200
        assert b"username,person_name,event_type,created_at" in response.data

        csv_text = """username,person_name,event_type,created_at,latitude,longitude,location_text
empone,Employee One,IN,2024-01-02 09:15:00,28.6139,77.2090,HQ Gate
empone,Employee One,OUT,2024-01-02 18:20:00,28.6139,77.2090,HQ Gate
"""
        csrf = get_csrf("/admin/analytics-page")
        response = client.post(
            "/admin/analytics/import",
            data={
                "_csrf_token": csrf,
                "analytics_csv": (io.BytesIO(csv_text.encode("utf-8")), "analytics.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert response.status_code == 200

        with app.app_context():
            imported = attendance_model.query.filter_by(entry_source="IMPORT").count()
            assert imported >= 2

        response = client.get("/dashboard?source=IMPORT")
        assert response.status_code == 200
        response = client.get("/admin/export?source=IMPORT")
        assert response.status_code == 200

        response = client.get("/logout", follow_redirects=False)
        assert response.status_code in (302, 303)

        csrf = get_csrf("/login")
        response = client.post(
            "/login",
            data={"username": "empone", "password": "EmpOne@123", "_csrf_token": csrf},
            follow_redirects=False,
        )
        assert response.status_code in (302, 303)

        csrf = get_csrf("/dashboard")
        response = client.post(
            "/attendance",
            json={
                "person_name": "Employee One",
                "action_type": "IN",
                "latitude": 28.6139,
                "longitude": 77.2090,
                "location_text": "HQ Gate",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200

        csrf = get_csrf("/dashboard")
        response = client.post(
            "/attendance",
            json={
                "person_name": "Employee One",
                "action_type": "OUT",
                "latitude": 28.6139,
                "longitude": 77.2090,
                "location_text": "HQ Gate",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
    finally:
        try:
            if os.path.exists(db_path):
                os.remove(db_path)
        except OSError:
            pass


if __name__ == "__main__":
    run_smoke_test()
    print("SMOKE_TESTS_OK")
