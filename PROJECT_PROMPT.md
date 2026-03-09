Build a production-grade, responsive Workforce Attendance SaaS with multi-tenant admin controls.

Core objective:
- Deliver an enterprise-ready attendance platform that can be sold per company with quota controls, analytics, and expansion modules.

Requirements:
1. Roles and hierarchy
- Three roles: `super_admin`, `admin` (company admin), `user` (employee).
- `super_admin` can create company admins, set account limits, and manage admin activation.
- `admin` can create/manage employee accounts only within their assigned quota and scope.
- `user` can mark attendance, view own records, and submit correction requests.

2. Tenant quota governance
- Each `admin` has a strict `user_limit` configured by `super_admin`.
- Employee creation must be blocked once limit is reached.
- Display used/remaining quota in admin UI.
- Track ownership via `company_admin_id` for data isolation.

3. Attendance flow
- Attendance form fields:
  - Person name (required)
  - Auto geolocation (required; clear fallback/error states)
  - Optional reverse-geocoded location label
  - IN/OUT action
- Save server timestamp automatically.
- Enforce shift/grace/overtime logic and optional geofence policy.

4. Data isolation and access control
- Company admins should only view/export/manage their own employees and their records.
- Correction approval and employee updates must be restricted to the same company scope.
- Main admin can view only quota-governance level controls, not employee-level daily operations by default.

5. Analytics and operations
- Dedicated analytics page with KPI cards + charts:
  - Hours by employee
  - Daily IN vs OUT
  - Performance scores
  - Department distribution
  - Employee trendline
- Admin dashboard should include:
  - Live in-office monitoring
  - Correction queue
  - Employee management
  - Bulk CSV onboarding (template download + import validation)
  - Export workflow

6. UI/UX quality bar
- Mobile-first + desktop optimized responsive behavior.
- Premium visual hierarchy (hero, KPI cards, action grouping, clear table workflows).
- Fast form interactions with clear loading/error/success feedback.
- Distinct icons for non-attendance expansion modules.

7. Expansion modules (productization)
- Show add-on modules with separate identity/icons:
  - Shift Planner
  - Payroll Sync
  - Visitor Desk
  - Compliance Vault
- Keep module messaging in UI and docs to support enterprise upsell positioning.

8. Security and platform standards
- Session-based authentication with password hashing.
- Role-based route protection across all sensitive endpoints.
- Server-side validation for all critical actions and limits.
- Graceful error handling for invalid payloads and unauthorized actions.

9. Tech stack
- Backend: Python + Flask + SQLAlchemy
- Database: SQLite (dev), Postgres-compatible in production
- Frontend: Jinja templates + vanilla JS + modern CSS
- Keep dependencies minimal and production-relevant.

10. Deliverables
- Working codebase with clean folder structure.
- Seeded main admin and company admin defaults documented in README.
- Setup, configuration, and deployment instructions.
- Maintainable, readable implementation with minimal but useful comments.
