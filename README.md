# AttendFlow - Premium Attendance & Workforce Ops

Production-ready attendance platform with geolocation-backed IN/OUT, policy controls, correction workflow, and advanced admin analytics.

## Key Features
- Main Admin (`super_admin`) control panel:
  - Create company admin accounts
  - Set per-admin employee creation limits
  - Set per-admin analytics access range (1 to 10 years)
  - Activate/deactivate company admin accounts
  - Reset company admin passwords
- Fast IN/OUT attendance flow optimized for mobile and desktop.
- Auto geolocation capture with optional strict geofence policy.
- Accurate working-hours calculation from multiple IN/OUT cycles per day.
- Employee correction request workflow (pending, approve, reject).
- Premium admin dashboard UX with:
  - Executive hero summary
  - Live workforce cards
  - Search/filter optimized attendance logs
  - Dedicated analytics page with KPI strip + charts
- Company admin quota governance:
  - Employee creation usage vs limit
  - Hard limit enforcement in backend
  - In-dashboard account creation workflow
- Self-registration disabled for security; onboarding is controlled by company admins.
- CSRF protection enabled for all state-changing requests (forms + JSON attendance API).
- Hardened session/cookie security defaults for production deployment.
- Security headers enabled (CSP, frame deny, nosniff, referrer policy).
- Login brute-force protection via rate limiting.
- Health endpoints for monitoring:
  - `GET /healthz`
  - `GET /readyz`
- Admin policy controls:
  - Shift start/end
  - Grace period
  - Full-day hours
  - Geofence center/radius/enforcement
- Employee management:
  - Profile metadata (employee code, department, designation, phone, joining date)
  - Active/inactive control
  - Employee password reset from admin table
  - Bulk CSV onboarding with downloadable template and row-level validation
- Scale optimizations for large tenants:
  - Bulk event aggregation for dashboard/analytics (reduced N+1 query pressure)
  - Chunked event loading to avoid DB parameter limits
  - Streaming CSV export to avoid high memory usage on large datasets
- Admin analytics (permissioned long-range):
  - Year-range analytics (1 to 10 years, as permitted by main admin)
  - Hours by employee
  - IN vs OUT trend
  - Performance scores
  - Department hour split
  - Employee trend
  - Historical attendance CSV import for analytics backfill
- Enterprise expansion modules (distinct non-attendance icons in UI):
  - Shift Planner (add-on)
  - Payroll Sync (add-on)
  - Visitor Desk (add-on)
  - Compliance Vault (add-on)
- Filtered CSV export (date/user/event/source).
- Responsive premium UI (desktop + mobile).

## Enterprise Pricing Justification (2L+ INR positioning)
- Compliance and audit readiness:
  - Full attendance history with source tracking and correction approval trail.
  - Geofence-backed attendance context reduces fraudulent or disputed punches.
- Operational savings:
  - Faster HR reconciliation through filtered exports and admin analytics.
  - Live visibility into in-office status, late arrivals, and overtime trends.
- Policy enforcement at source:
  - Shift/grace/full-day/geofence rules built into attendance operations.
- Expansion-led revenue strategy:
  - Add-on modules (shift planning, payroll sync, visitor, compliance) enable per-industry packaging and upsell.

## Suggested High-Value Add-ons to Build Next
- Payroll engine connector layer (Tally/Zoho/SAP/Workday adapters).
- Shift planning with approvals, swaps, and overtime budgeting.
- Leave and holiday policy engine with attendance reconciliation.
- Contractor and visitor access workflow with host approvals.
- Compliance pack generator (monthly/quarterly audit-ready reports).
- Webhooks/API gateway for ERP and HRMS integrations.

## Tech Stack
- Python 3.11+
- Flask + Flask-SQLAlchemy
- SQLAlchemy ORM
- SQLite (local) or Postgres (`DATABASE_URL`)
- Jinja templates + Vanilla JS + Chart.js + CSS

## Local Setup
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Run app:
   ```bash
   python app.py
   ```
3. Open:
   ```text
   http://127.0.0.1:5000
   ```
4. Run production smoke test:
   ```bash
   python scripts/smoke_test.py
   ```
5. Run crash route scan:
   ```bash
   python scripts/crash_scan.py
   ```

## Environment Variables
- `SECRET_KEY` (strongly recommended in production; if missing, app uses a temporary fallback key and sessions may reset)
- `DATABASE_URL` (recommended in production; Vercel Postgres)
- `SQLITE_DB_PATH` (optional SQLite file path override for local/self-hosted deployments)
- `APP_TIMEZONE` (example: `Asia/Kolkata`)
- `APP_ENV` / `FLASK_ENV` / `VERCEL_ENV` (set to `production` in production)
- `FLASK_DEBUG` (set `0` in production)
- `SESSION_COOKIE_SECURE` (recommended `1` in production)
- `SESSION_COOKIE_SAMESITE` (default: `Lax`)
- `SESSION_TTL_HOURS` (default: `12`)
- `MAX_CONTENT_LENGTH_MB` (default: `7`)
- `SQLITE_BUSY_TIMEOUT_SECONDS` (default: `30`, SQLite lock wait timeout)
- `LOGIN_RATE_LIMIT_WINDOW_SECONDS` (default: `300`)
- `LOGIN_RATE_LIMIT_MAX_ATTEMPTS` (default: `12`)
- `TRUST_PROXY_HEADERS` (default: `1`)
- `DISABLE_CSP` (default: `0`, keep disabled only for debugging CSP issues)
- `DEFAULT_SUPER_ADMIN_USERNAME` (optional, default: `superadmin`)
- `DEFAULT_SUPER_ADMIN_PASSWORD` (optional, default: `Admin@123`)
- `DEFAULT_SUPER_ADMIN_NAME` (optional, default: `Main Administrator`)
- `DEFAULT_COMPANY_ADMIN_USERNAME` (optional, default: `admin`)
- `DEFAULT_COMPANY_ADMIN_PASSWORD` (optional, default: `Admin@123`)
- `DEFAULT_COMPANY_ADMIN_NAME` (optional, default: `Company Administrator`)
- `DEFAULT_COMPANY_ADMIN_LIMIT` (optional, default: `50`)
- `DEFAULT_COMPANY_ADMIN_ANALYTICS_YEARS` (optional, default: `1`, range `1-10`)
- `ANALYTICS_IMPORT_ROW_LIMIT` (optional, default: `20000`)
- `COMPANY_NAME` (optional)

When `DATABASE_URL` is not set, the app auto-selects a writable SQLite location.  
Default priority:
1. `data/attendance.db`
2. `attendance.db` in project root
3. system temp directory

## Vercel Deployment
This repo is already configured with:
- `vercel.json`
- `api/index.py`

### Vercel UI commands
Use these values in Project Settings:
- Build Command: `pip install -r requirements.txt`
- Output Directory: leave blank
- Install Command: `pip install -r requirements.txt`
- Development Command: `python app.py`

### Recommended production steps
1. Push code to GitHub.
2. Import repo in Vercel.
3. Add Vercel Postgres and set `DATABASE_URL` (strongly recommended).
4. Set `SECRET_KEY` (required for stable production sessions) and `APP_TIMEZONE`.
5. Set strong boot passwords before first deploy:
   - `DEFAULT_SUPER_ADMIN_PASSWORD`
   - `DEFAULT_COMPANY_ADMIN_PASSWORD`
6. Deploy.

Notes:
- If `DATABASE_URL` is not set on Vercel, app falls back to serverless temp SQLite storage, which is ephemeral.
- For production data persistence and multi-instance reliability, always use Postgres on Vercel.

## Security Notes
- Passwords are hashed.
- Admin credentials are not displayed in UI.
- Coordinate ranges are validated.
- Role-based access is enforced for admin routes.

## Project Structure
- `app.py` - Flask app, models, routes, business logic
- `api/index.py` - Vercel entrypoint
- `templates/` - Jinja templates
- `static/js/` - Frontend scripts
- `static/css/` - Styling
- `data/` - local SQLite DB (dev only)
