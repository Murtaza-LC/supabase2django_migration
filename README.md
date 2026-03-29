# Supabase → Django Migration Playbook

A step-by-step guide for migrating a production application from **Supabase** (PostgreSQL + Edge Functions + Auth) to a **Django REST Framework** backend — including schema extraction, model generation, app splitting, DRF scaffolding, seed data loading, and frontend API migration.

This repo documents the migration of our platform which has 109 database tables, ~1,950 columns, complex RLS, multi-tenancy, and Supabase Edge Functions — migrated to Django 5.x + DRF + Celery + PostgreSQL.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Prerequisites](#prerequisites)
- [Phase 1 — Extract Schema & Data from Supabase](#phase-1--extract-schema--data-from-supabase)
- [Phase 2 — Reconstruct the Database Locally](#phase-2--reconstruct-the-database-locally)
- [Phase 3 — Generate Django Models via inspectdb](#phase-3--generate-django-models-via-inspectdb)
- [Phase 4 — Reconcile Field Types](#phase-4--reconcile-field-types)
- [Phase 5 — Split Models into Django Apps](#phase-5--split-models-into-django-apps)
- [Phase 6 — Generate Migrations](#phase-6--generate-migrations)
- [Phase 7 — Scaffold DRF (Serializers, Views, URLs)](#phase-7--scaffold-drf-serializers-views-urls)
- [Phase 8 — Migrate Edge Functions → Django/Celery](#phase-8--migrate-edge-functions--djangocelery)
- [Phase 9 — Migrate the Frontend](#phase-9--migrate-the-frontend)
- [Phase 10 — Load Seed Data](#phase-10--load-seed-data)
- [Helper Scripts Reference](#helper-scripts-reference)
- [Common Gotchas](#common-gotchas)
- [Verification Checklist](#verification-checklist)

---

## Architecture Overview

```
Before                              After
──────────────────────────          ──────────────────────────────────
React/TS Frontend                   React/TS Frontend
   │                                   │
   ├─► Supabase Auth                   ├─► Django Auth (JWT)
   ├─► Supabase Edge Functions         ├─► Django REST Framework (v1)
   ├─► Supabase Realtime               ├─► Django Channels (WebSocket)
   └─► Supabase Storage                ├─► Celery + Redis (async tasks)
                                       └─► AWS S3 (file storage)
```

**Django app structure** (logical business domains, not one-model-per-file):

```
apps/
├── app1/              # X systems, model lifecycle, RAG, x models
├── app2/              # Audit logs, validation metrics, monitoring
├── app3/              # Tenants, user profiles, operator metadata
├── x_auth/            # Authentication, user identity
├── ...
```

---

## Prerequisites

- Python 3.11+, pip, virtualenv
- PostgreSQL 15+ installed locally
- Docker + Docker Compose (for running the full stack)
- Node.js 18+ (for frontend migration script)
- Supabase project with an active Edge Function for data export

## Platform Compatibility
Scripts are tested on **macOS** and **Linux**. Windows users will need WSL2 
or to adapt the shell commands manually.

## Who is this for?

This repo is for developers and teams who:

- already understand Supabase core functionality and have used it in a real project
- have working knowledge of Django ORM, migrations, and DRF ViewSets
- are comfortable reading and adapting Python, shell, and JavaScript scripts for their own project
- want a practical migration playbook based on a real migration journey

## Who is this not for?

This repo may not be the best fit if:

- you are completely new to both Supabase and Django
- your project is small enough to rebuild manually
- you are looking for a one-click migration tool OR you need a fully automated, zero-touch migration (this playbook still requires judgment calls and manual fixes)
- your target backend is not Django / DRF

---

## Phase 1 — Extract Schema & Data from Supabase

Supabase does not expose a direct `pg_dump`. Deploy a short-lived Edge Function to stream a full SQL dump, then download it.

```bash
# Download the full dump (schema + data) from your Supabase Edge Function
curl "https://<your-project>.supabase.co/functions/v1/db-full-dump" \
  -H "Authorization: Bearer <your-service-role-key>" \
  -o full_dump.sql
```

Then split into separate schema and data files (makes it easier to reload independently):

Your Typical File should contain

- EXTENSIONS
- CUSTOM ENUM TYPES
- CREATE TABLE (without FK constraints)
- FOREIGN KEY CONSTRAINTS
- INDEXES
- DATA (INSERT statements)

```bash
# For example, if your file has Tables, Alter Tables and Indexes together followed by INSERT INTO statements 
# You can use these commands, it will split everything up to the first INSERT INTO → schema
grep -n "INSERT INTO" full_dump.sql | head -1
# Use that line number to split, e.g. line 4821:
head -n 4820 full_dump.sql > full_schema.sql
tail -n +4821 full_dump.sql > full_data.sql
```

---

## Phase 2 — Reconstruct the Database Locally

Supabase manages `auth.*` tables internally and they are not exported cleanly. Recreate the minimal auth schema stub before importing.

```bash
# Create the local database
createdb -U postgres test_x

# Supabase Auth stub — must be created manually before schema import
psql -U postgres -d test_x -c "
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (
  instance_id uuid,
  id uuid PRIMARY KEY,
  aud varchar(255),
  role varchar(255),
  email text,
  encrypted_password text,
  email_confirmed_at timestamptz,
  created_at timestamptz,
  updated_at timestamptz,
  confirmation_token text,
  recovery_token text
);"

# Import schema (captures all errors to a log for review)
psql -a -e -U postgres -d test_x < full_schema.sql > import_debug.log 2>&1

# Verify tables were created
psql -U postgres -d test_x -c "\dt"
psql -U postgres -d test_x -c "\dt auth.*"

# Spot-check a table
psql -U postgres -d test_x -c "SELECT COUNT(*) FROM public.user_roles;"
psql -U postgres -d test_x -c "\dt+ user_roles"
```

> **Tip:** Review `import_debug.log` for errors. Most will be related to Supabase-specific extensions (`pg_graphql`, `supabase_vault`) — these are safe to ignore.

---

## Phase 3 — Generate Django Models via inspectdb

With the database populated, point Django at it and generate raw models.

```bash
# Activate your virtualenv
source venv/bin/activate

# Confirm Django is pointed at the right DB
python manage.py shell --settings=config.settings.local \
  -c "from django.conf import settings; print(settings.DATABASES['default']['NAME'])"

# Generate all models from the live DB into a single file
python manage.py inspectdb > master_models.py
```

This produces a `master_models.py` with `managed = False` set on every model. The next phases will split, correct, and re-enable management.

---

## Phase 4 — Reconcile Field Types

`inspectdb` makes educated guesses. Cross-reference your frontend integration types (from `./integrations/index.ts` or similar) to catch mismatches — especially `text` vs `uuid`, and absurd `DecimalField` precision values.

**Fix common inspectdb artefacts across all models in one pass:**

```bash
# inspectdb sometimes emits max_digits=65535 for Decimal fields — PostgreSQL's
# internal representation. Fix these to sane values:
grep -r "max_digits=65535" ./apps

find ./apps -name "models.py" -exec sed -i '' 's/max_digits=65535/max_digits=20/g' {} +
find ./apps -name "models.py" -exec sed -i '' 's/decimal_places=65535/decimal_places=10/g' {} +
```

**Diff DB vs models after any manual edits:**

```bash
# Re-run inspectdb against the live DB and compare against your hand-edited models
python manage.py inspectdb > inspectdb_output.py
python3 diff_models.py inspectdb_output.py django_backend/apps/
```

`diff_models.py` reports:
- Fields in DB missing from `models.py`
- Fields in `models.py` missing from DB (orphaned)
- Type mismatches between inspectdb and your models
- Models in DB with no matching Django class (and vice versa)

Review `diff_models_report.txt` and resolve each section before proceeding.

---

## Phase 5 — Split Models into Django Apps

Run `splitmodels.py` to route each model class into a logical app, create the Django app skeleton (via `manage.py startapp`), fix `apps.py` import paths, update all FK/M2M/OneToOne references to use cross-app string notation, and flip `managed = True`.

```bash
python splitmodels.py
```

The routing logic in `get_unified_app()` maps table-name prefixes to logical domains:

| Prefix pattern | App |
|---|---|
| `domain1_` | `model_`, `....' |
| `domain2_` | `pattern1_`, `pattern2_` | `pattern3_` |
| `domain3_` | `pattern1_`, `pattern2_` | `pattern3_` |
| `user_` | `tenant_`, `profile_`,  `...` |
| `users` (exact) | `x_auth` |
| `datasets` (exact), everything else | `x_engine` |

After running:
- Each app directory is created under `apps/` with full Django skeleton
- `apps.py` is updated with `name = 'apps.<app_name>'` and `verbose_name`
- `models.py` in each app contains only its models, with corrected cross-app FK strings
- `apps_to_register.txt` is generated — paste its contents into `INSTALLED_APPS`

**Verify model class placement:**

```bash
python3 check_class_locations.py --project-root . --json-out class_locations.json
```

**Verify import correctness across all files:**

```bash
python3 check_model_imports.py apps/ --recursive --json-out import_check.json
```

---

## Phase 6 — Generate Migrations

```bash
# Register all new apps in INSTALLED_APPS first (use apps_to_register.txt)

# Check what Django sees
python manage.py showmigrations

# Generate migrations for all apps
python manage.py makemigrations

# Use --fake-initial because the tables already exist in the DB
python manage.py migrate --fake-initial

# Verify
python manage.py showmigrations

# If a specific app has issues, target it individually
python manage.py makemigrations <app_name>
python manage.py migrate <app_name>

# Confirm Django and DB are in sync (both should exit clean with no output)
python manage.py makemigrations --check
python manage.py migrate --check
```

---

## Phase 7 — Scaffold DRF (Serializers, Views, URLs)

```bash
python generate_drf.py
```

For each app, this generates:

- **`serializers.py`** — `ModelSerializer` with `fields = '__all__'` per model
- **`views.py`** — `TenantAwareModelViewSet` (inherits from a custom base that enforces Row-Level Security / tenant isolation)
- **`urls.py`** — `DefaultRouter` registration with kebab-case URL slugs

Sensitive models (`Users`, `AuthUsers`) are excluded from the generated ViewSets by a configurable blocklist.

> **Important:** You must create `apps/core/views.py` with `TenantAwareModelViewSet` before the generated views will import correctly. This class should override `get_queryset()` to filter by the current tenant.

**Verify the Django project is healthy:**

```bash
python manage.py check --settings=config.settings.local
python manage.py runserver
```

**List all registered API routes:**

```bash
python -c "
import sys, os
sys.path.insert(0, 'django_backend')
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings.local'
import django
django.setup()
from django.urls import get_resolver
resolver = get_resolver()
def list_urls(patterns, prefix=''):
    for p in patterns:
        if hasattr(p, 'url_patterns'):
            list_urls(p.url_patterns, prefix + str(p.pattern))
        else:
            print(prefix + str(p.pattern))
list_urls(resolver.url_patterns)
" 2>&1 | grep "api/v1" | sort | head -200
```

**Test specific URL resolution:**

```bash
python -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')
django.setup()
from django.urls import resolve, Resolver404

urls_to_test = [
    '/api/v1/domain1/function1/',
    '/api/v1/domain2/function1/',
]

for url in urls_to_test:
    try:
        m = resolve(url)
        print(f'OK  {url}  -> {m.func}')
    except Resolver404:
        print(f'404 {url}')
" 2>&1 | tail -20
```

**Syntax-check generated files:**

```bash
python3 -c "
import ast
for f in [
    'django_backend/apps/domain1/serializers.py',
    'django_backend/apps/domain1/views.py',
    'django_backend/apps/domain1/urls.py',
]:
    try:
        ast.parse(open(f).read())
        print(f'{f}: OK')
    except SyntaxError as e:
        print(f'{f}: SYNTAX ERROR - {e}')
"
```

---

## Phase 8 — Migrate Edge Functions → Django/Celery

Each Supabase Edge Function (Deno/TypeScript) is converted to one of:

- A **Django view** — for synchronous, request/response logic
- A **Celery task** — for async, long-running, or scheduled work
- A **Lambda stub** — for workloads that will move to AWS Lambda post-migration

**Conversion pattern:**

| Supabase pattern | Django equivalent |
|---|---|
| `supabase.rpc('fn_name', params)` | `apiClient.post('/rpc/fn-name/', params)` |
| `supabase.functions.invoke('fn')` | `apiClient.post('/functions/fn/')` |
| `supabase.auth.getUser()` | `useAuth()` hook / `request.user` |
| `supabase.from('table').select()` | DRF ViewSet / `Model.objects.filter()` |
| Scheduled Edge Function | Celery Beat periodic task |

**Celery task skeleton:**

```python
from celery import shared_task

@shared_task(bind=True, max_retries=3)
def process_evidence_collection(self, system_id: str, tenant_id: str):
    """Replaces: supabase/functions/func_1/index.ts"""
    try:
        # ... business logic
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60)
```

---

## Phase 9 — Migrate the Frontend

Run the automated migration script to replace Supabase client calls with Django API calls across all TypeScript/TSX files:

```bash
# Dry run first — shows what would change without modifying files
node migrate_supabase.js --dry-run

# Apply migrations (creates .backup files for every modified file)
node migrate_supabase.js
```

**What it rewrites automatically:**

| Pattern | Replacement |
|---|---|
| `supabase.auth.getSession()` | `useAuth()` hook |
| `supabase.auth.getUser()` | `useAuth()` hook |
| `supabase.auth.signOut()` | `logout()` from `useAuth` |
| `supabase.rpc('fn', params)` | `apiClient.post('/rpc/fn/', params)` |
| `supabase.functions.invoke('fn', { body })` | `apiClient.post('/functions/fn/', body)` |
| `supabase.from('table').select().eq()` | `apiClient.get('/table/', { params })` |
| `supabase.from('table').insert(data)` | `apiClient.post('/table/', data)` |

**Find remaining frontend → backend URL mismatches:**

```bash
# Collect all apiClient URLs used in the frontend
python3 << 'EOF'
import re, os
from collections import defaultdict

api_calls = set()
calls_by_url = defaultdict(list)

for root, dirs, files in os.walk('src'):
    dirs[:] = [d for d in dirs if d != 'node_modules']
    for file in files:
        if file.endswith(('.ts', '.tsx')):
            filepath = os.path.join(root, file)
            try:
                content = open(filepath).read()
                matches = re.findall(
                    r"apiClient\.(get|post|put|patch|delete)\(['\"]([^'\"]+)['\"]",
                    content
                )
                for method, url in matches:
                    api_calls.add(url)
                    calls_by_url[url].append(filepath.replace('src/', ''))
            except:
                pass

for url in sorted(api_calls):
    files = calls_by_url[url]
    print(f"{url:60} (from: {', '.join(files[:2])})")
EOF
```

After migration:
```bash
# Review all changes
git diff

# Check for TypeScript errors
npm run type-check

# Test the build
npm run build
```

---

## Phase 10 — Load Seed Data

The seed file from Supabase is large and may have dependency ordering requirements. Update the provided script to create split and verification scripts.

**Step 1 — Split the seed file into ordered load batches:**

```bash
python3 split_seed.py full_data.sql
```

This produces:
- `seed_01_prereq.sql` — prereqdomain1 prereqdomain2 (no dependencies)
- `seed_02_prereq2.sql` — prereqdomain2
- `seed_03_requirements.sql` — prereqdomain3
...
- `seed_06_everything_else.sql` — all remaining tenant data
- `seed_SKIPPED.sql` — If needed you can have this section for rows referencing archived/deleted records (do not load)

**Step 2 — Load in order:**

```bash
for f in seed_01 seed_02 seed_03 seed_04 seed_05 seed_06; do
  docker compose exec -T db psql -U django -d django_backend \
    -v ON_ERROR_STOP=1 < ${f}_*.sql
done
```

**Step 3 — Verify row counts:**

```bash
python3 verify_seed.py full_data.sql
docker compose exec -T db psql -U django -d django_backend < verify_seed.sql
```

The verification query shows `expected`, `actual`, and `gap` columns per table — `gap = 0` means fully loaded.

**Alternative: run a quick rollback-safe test load:**

```bash
PGHOST=HOST PGPORT=5432 PGDATABASE=DATABASENAME \
PGUSER=USER PGPASSWORD=<your_password> \
python3 -c "
from pathlib import Path
import os, psycopg

sql = Path('seed_06_everything_else.sql').read_text(encoding='utf-8')
conn = psycopg.connect(
    host=os.environ['PGHOST'], port=os.environ['PGPORT'],
    dbname=os.environ['PGDATABASE'], user=os.environ['PGUSER'],
    password=os.environ['PGPASSWORD']
)
cur = conn.cursor()
cur.execute(sql)
conn.rollback()   # ← safe: rolls back, just validates syntax/FK integrity
cur.close(); conn.close()
print('SQL executed successfully and rolled back.')
"
```

> **Note:** Seed loading is the most manual phase. You will encounter FK violations, missing referenced rows, and data type mismatches. Fix these iteratively — the verification query is your guide.

---

## Helper Scripts Reference

| Script | Purpose |
|---|---|
| [`splitmodels.py`](./scripts/splitmodels.py) | Splits `master_models.py` into per-app `models.py` files, creates Django app skeletons, fixes cross-app FK strings |
| [`generate_drf.py`](./scripts/generate_drf.py)` | Generates `serializers.py`, `views.py`, `urls.py` for each app |
| [`diff_models.py`](./scripts/diff_models.py) | Diffs `inspectdb` output against hand-edited `models.py` files; reports missing fields, orphans, type mismatches |
| [`check_class_locations.py`](./scripts/check_class_locations.py) | Validates that specific model classes are in the expected app |
| [`check_model_imports.py`](./scripts/check_model_imports.py) | Scans files for `from apps.<app>.models import ...` and verifies the class is actually defined there |
| [`split_seed.py`](./scripts/split_seed.py) | Splits Supabase SQL export into dependency-ordered load files; skips archived rows |
| [`verify_seed.py`](./scripts/verify_seed.py) | Parses seed file to count expected rows per table; generates a SQL query to compare against DB actuals |
| [`migrate_supabase.js`](./scripts/migrate_supabase.js) | Node.js script to rewrite Supabase client calls to Django API calls across all frontend TypeScript files |

---

## Common Gotchas

### DecimalField precision from inspectdb

```bash
# inspectdb emits PostgreSQL's internal max — replace with sensible values
find ./apps -name "models.py" -exec sed -i '' 's/max_digits=65535/max_digits=20/g' {} +
find ./apps -name "models.py" -exec sed -i '' 's/decimal_places=65535/decimal_places=10/g' {} +
```

### Cross-app ForeignKey strings

After splitting, all relations between apps must use string notation:
```python
# ✗ Won't work across apps
user = models.ForeignKey(Users, on_delete=models.CASCADE)

# ✓ Correct cross-app string reference
user = models.ForeignKey('gov_auth.Users', on_delete=models.CASCADE)
```
`splitmodels.py` handles this automatically for `ForeignKey`, `ManyToManyField`, and `OneToOneField`.

### Supabase Auth tables

The `auth.users` table is managed by Supabase internally and is not in your public schema dump. You must recreate the stub manually (see Phase 2) before importing the schema, otherwise FK constraints from your `users` or `profiles` table will fail.

### `managed = False` after inspectdb

`inspectdb` sets `managed = False` on every model to prevent Django from touching the existing schema. `splitmodels.py` flips these to `managed = True`. If you're doing `--fake-initial` migrations on a pre-existing DB, this is correct behaviour — Django will record the migration state without running `CREATE TABLE`.

### app label in `apps.py`

Django requires `name = 'apps.<app_name>'` (not just `'<app_name>'`) when apps live under an `apps/` subdirectory. `splitmodels.py` patches this automatically via `fix_app_config()`.

### Seed FK ordering

Supabase exports rows in table-alphabetical order, not dependency order. Loading directly will fail on FK constraints. Always use `split_seed.py` to reorder, and always use `ON_ERROR_STOP=1` so you catch the first failure immediately.

---

## Verification Checklist

**Schema:**
- [ ] `python manage.py makemigrations --check` exits clean (no changes detected)
- [ ] `python manage.py migrate --check` exits clean (DB matches migration history)
- [ ] `python manage.py check` returns no errors
- [ ] `diff_models.py` shows no unresolved sections
- [ ] `check_class_locations.py` and `check_model_imports.py` show no cross-app import or model location errors

**Data:**
- [ ] `verify_seed.sql` shows `gap = 0` for all non-skipped tables
- [ ] Spot-check 5 representative tables with `SELECT COUNT(*)`

**API:**
- [ ] `python manage.py runserver` starts without errors
- [ ] All routes in `api/v1/` resolve correctly
- [ ] Health endpoints return 200: `/health/live`, `/health/ready`

**Frontend:**
- [ ] `migrate-supabase.js` has been run and all Supabase client calls rewritten
- [ ] `npm run type-check` passes
- [ ] `npm run build` succeeds
- [ ] No remaining `supabase.` references in `src/` (except the client config file itself)

```bash
# Find any remaining Supabase calls
grep -r "supabase\." src/ --include="*.ts" --include="*.tsx" \
  | grep -v ".backup" | grep -v "supabaseClient.ts"
```

---

## Create a Django Superuser

```bash
cd django_backend
python manage.py createsuperuser
```

---

## License

MIT
