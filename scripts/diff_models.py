"""
Author: Murtaza Nuruddin
diff_models.py

Compares Django's inspectdb output (what the DB actually has) against your
existing models.py files (what Django thinks the schema is).

Flags:
  - Fields in DB (inspectdb) but missing from models.py
  - Fields in models.py but missing from DB (orphaned — may be safe or risky)
  - Fields present in both but with different types
  - Models (tables) in DB but not in any models.py
  - Models in models.py but not in DB

Does NOT modify any files. Output only.

Prerequisites:
  - Run inspectdb first and save the output:
      python manage.py inspectdb > inspectdb_output.py
      OR
      docker compose exec backend python manage.py inspectdb > inspectdb_output.py

  - Have your Django app models.py files accessible locally.

Usage:
    python3 diff_models.py inspectdb_output.py path/to/django_backend/apps/

The second argument is the root folder containing your Django apps.
The script will find every models.py under that path automatically.

Output:
  diff_models_report.txt  — full diff report
  (also printed to terminal)
"""

"""
# Step 1 — generate inspectdb from the live DB
docker compose exec backend python manage.py inspectdb > inspectdb_output.py

# Step 2 — run the diff
python3 diff_models.py inspectdb_output.py django_backend/apps/

# Step 3 — review diff_models_report.txt and update models.py files

# Step 4 — verify Django agrees (should exit clean)
docker compose exec backend python manage.py makemigrations --check

# Step 5 — verify DB matches migration history (should exit clean)
docker compose exec backend python manage.py migrate --check

# Step 6 — full structural audit
python3 verify_structure.py seed_fixed.sql
docker compose exec -T db psql -U django -d django_backend < verify_structure.sql > structure_report.txt 2>&1

"""

import ast
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Django field type normalisation ──────────────────────────────────────────
# inspectdb and hand-written models use slightly different field names for the
# same underlying type. This maps both to a canonical name so comparisons are
# fair rather than flagging spurious differences.
#
# Key  = what you might see in inspectdb OR models.py
# Value = canonical name used in the diff report

FIELD_TYPE_ALIASES = {
    # Text
    "TextField":        "text",
    "CharField":        "text",
    "SlugField":        "text",
    "EmailField":       "text",
    "URLField":         "text",
    # Numbers
    "IntegerField":     "integer",
    "SmallIntegerField":"integer",
    "BigIntegerField":  "biginteger",
    "PositiveIntegerField": "integer",
    "PositiveBigIntegerField": "biginteger",
    "FloatField":       "float",
    "DecimalField":     "decimal",
    # Boolean
    "BooleanField":     "boolean",
    "NullBooleanField": "boolean",
    # Date / time
    "DateField":        "date",
    "DateTimeField":    "datetime",
    "TimeField":        "time",
    # Binary / UUID
    "UUIDField":        "uuid",
    "BinaryField":      "binary",
    # JSON
    "JSONField":        "json",
    # Relations — inspectdb uses ForeignKey; hand-written models also use it.
    # We keep ForeignKey as-is so we can spot when inspectdb has a raw UUIDField
    # where models.py has a ForeignKey (a common drift pattern).
    "ForeignKey":       "fk",
    "OneToOneField":    "fk",
    "ManyToManyField":  "m2m",
    # Auto
    "AutoField":        "auto",
    "BigAutoField":     "auto",
    "SmallAutoField":   "auto",
}


def normalise_type(type_name: str) -> str:
    return FIELD_TYPE_ALIASES.get(type_name, type_name.lower())


# ── Data containers ───────────────────────────────────────────────────────────

@dataclass
class FieldDef:
    name: str
    field_type: str          # raw type name
    canonical_type: str      # normalised for comparison
    source_file: str         # which file this came from
    extra: str = ""          # raw args string for context


@dataclass
class ModelDef:
    name: str
    db_table: Optional[str]  # from Meta.db_table or None
    fields: dict             # {field_name: FieldDef}
    source_file: str


# ── Parser: inspectdb output ─────────────────────────────────────────────────

def parse_inspectdb(filepath: str) -> dict:
    """
    Parse the raw text output of `manage.py inspectdb`.
    Returns {model_name: ModelDef}
    """
    models = {}
    current_model = None
    current_fields = {}
    current_db_table = None

    # inspectdb field line looks like:
    #   field_name = models.TextField(blank=True, null=True)
    field_re = re.compile(
        r'^\s{4}(\w+)\s*=\s*models\.(\w+)\((.*)$'
    )
    # class declaration
    class_re = re.compile(r'^class (\w+)\(models\.Model\):')
    # db_table in Meta
    dbtable_re = re.compile(r"db_table\s*=\s*['\"](\w+)['\"]")

    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    for line in lines:
        class_match = class_re.match(line)
        if class_match:
            # Save previous model
            if current_model:
                models[current_model] = ModelDef(
                    name=current_model,
                    db_table=current_db_table,
                    fields=current_fields,
                    source_file=filepath
                )
            current_model  = class_match.group(1)
            current_fields = {}
            current_db_table = None
            continue

        if current_model:
            dbtable_match = dbtable_re.search(line)
            if dbtable_match:
                current_db_table = dbtable_match.group(1)

            field_match = field_re.match(line)
            if field_match:
                fname      = field_match.group(1)
                ftype      = field_match.group(2)
                fargs      = field_match.group(3).rstrip(')')
                if fname not in ('class', 'objects'):
                    current_fields[fname] = FieldDef(
                        name=fname,
                        field_type=ftype,
                        canonical_type=normalise_type(ftype),
                        source_file=filepath,
                        extra=fargs
                    )

    # Save last model
    if current_model:
        models[current_model] = ModelDef(
            name=current_model,
            db_table=current_db_table,
            fields=current_fields,
            source_file=filepath
        )

    return models


# ── Parser: existing models.py files ─────────────────────────────────────────

def parse_models_file(filepath: str) -> dict:
    """
    Parse a Django models.py using the AST for accuracy.
    Returns {model_name: ModelDef}
    """
    models = {}

    with open(filepath, 'r', encoding='utf-8') as f:
        source = f.read()

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        print(f"  [WARN] Could not parse {filepath}: {e}")
        return {}

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        # Only process classes that inherit from models.Model (directly or via
        # intermediate base — check for any base named 'Model' or ending in 'Model')
        bases = [
            (b.attr if isinstance(b, ast.Attribute) else
             b.id   if isinstance(b, ast.Name) else "")
            for b in node.bases
        ]
        if not any('Model' in b for b in bases):
            continue

        model_name  = node.name
        fields      = {}
        db_table    = None

        for item in node.body:
            # Field assignments: field_name = models.SomeField(...)
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    fname = target.id
                    if fname.startswith('_'):
                        continue

                    # Extract field type from the value
                    val = item.value
                    ftype = None
                    fargs = ""

                    if isinstance(val, ast.Call):
                        func = val.func
                        if isinstance(func, ast.Attribute):
                            ftype = func.attr
                        elif isinstance(func, ast.Name):
                            ftype = func.id

                    if ftype and ftype.endswith('Field') or ftype in (
                        'ForeignKey', 'OneToOneField', 'ManyToManyField'
                    ):
                        fields[fname] = FieldDef(
                            name=fname,
                            field_type=ftype,
                            canonical_type=normalise_type(ftype),
                            source_file=filepath,
                            extra=fargs
                        )

            # Meta class — look for db_table
            if isinstance(item, ast.ClassDef) and item.name == 'Meta':
                for meta_item in item.body:
                    if isinstance(meta_item, ast.Assign):
                        for t in meta_item.targets:
                            if isinstance(t, ast.Name) and t.id == 'db_table':
                                if isinstance(meta_item.value, ast.Constant):
                                    db_table = meta_item.value.value

        models[model_name] = ModelDef(
            name=model_name,
            db_table=db_table,
            fields=fields,
            source_file=filepath
        )

    return models


def find_models_files(apps_root: str) -> list:
    """Recursively find all models.py files under apps_root."""
    found = []
    for root, dirs, files in os.walk(apps_root):
        # Skip migrations folders — they contain model-like classes we don't want
        dirs[:] = [d for d in dirs if d != 'migrations' and not d.startswith('.')]
        for f in files:
            if f == 'models.py':
                found.append(os.path.join(root, f))
    return sorted(found)


# ── Matching: link inspectdb models to models.py models ──────────────────────

def build_table_to_model_map(project_models: dict) -> dict:
    """
    Build a lookup from db_table name → ModelDef for all project models.
    Falls back to snake_casing the model class name if no db_table is set.
    """
    mapping = {}
    for model_name, mdef in project_models.items():
        if mdef.db_table:
            mapping[mdef.db_table] = mdef
        else:
            # Django default: CamelCase → camel_case
            snake = re.sub(r'(?<!^)(?=[A-Z])', '_', model_name).lower()
            mapping[snake] = mdef
    return mapping


# ── Diff engine ───────────────────────────────────────────────────────────────

@dataclass
class DiffResult:
    # Model-level issues
    in_db_not_in_models:  list = field(default_factory=list)  # (db_table, inspectdb_model)
    in_models_not_in_db:  list = field(default_factory=list)  # (model_name, source_file)

    # Field-level issues per table
    # Each entry: (table, field_name, issue_type, detail)
    field_issues: list = field(default_factory=list)


def diff(inspectdb_models: dict, project_models: dict) -> DiffResult:
    result = DiffResult()

    # Build a map of db_table → project ModelDef
    table_map = build_table_to_model_map(project_models)

    # ── Check every inspectdb model against project models ───────────────────
    for idb_model_name, idb_model in inspectdb_models.items():
        # The db_table from inspectdb is the authoritative table name
        db_table = idb_model.db_table or idb_model_name

        # Skip Django internal tables
        if db_table.startswith('django_') or db_table.startswith('auth_'):
            continue

        proj_model = table_map.get(db_table)

        if proj_model is None:
            # Table exists in DB but no matching model found
            result.in_db_not_in_models.append((db_table, idb_model))
            continue

        # Compare fields
        idb_fields  = idb_model.fields
        proj_fields = proj_model.fields

        for fname, idb_field in idb_fields.items():
            if fname in ('id',):
                continue  # skip PK — always present, rarely useful to diff

            proj_field = proj_fields.get(fname)

            if proj_field is None:
                result.field_issues.append((
                    db_table,
                    fname,
                    'MISSING_FROM_MODELS',
                    f"DB has {idb_field.field_type} — not in models.py "
                    f"({proj_model.source_file})"
                ))
            elif proj_field.canonical_type != idb_field.canonical_type:
                # Type mismatch — only flag if not a known safe equivalence
                result.field_issues.append((
                    db_table,
                    fname,
                    'TYPE_MISMATCH',
                    f"DB:{idb_field.field_type} vs models.py:{proj_field.field_type} "
                    f"(in {proj_model.source_file})"
                ))

        # Fields in models.py but NOT in DB
        for fname, proj_field in proj_fields.items():
            if fname not in idb_fields:
                result.field_issues.append((
                    db_table,
                    fname,
                    'IN_MODELS_NOT_IN_DB',
                    f"models.py has {proj_field.field_type} but column not found in DB "
                    f"(migration missing or column dropped?) "
                    f"[{proj_model.source_file}]"
                ))

    # ── Check project models with no matching DB table ────────────────────────
    idb_tables = {
        (m.db_table or m.name): True
        for m in inspectdb_models.values()
    }
    for model_name, mdef in project_models.items():
        db_table = mdef.db_table or re.sub(r'(?<!^)(?=[A-Z])', '_', model_name).lower()
        if db_table not in idb_tables:
            result.in_models_not_in_db.append((model_name, mdef.source_file, db_table))

    return result


# ── Report formatter ──────────────────────────────────────────────────────────

def format_report(result: DiffResult, inspectdb_models: dict, project_models: dict) -> str:
    lines = []

    lines.append("=" * 70)
    lines.append("MODELS DIFF REPORT")
    lines.append("inspectdb (live DB)  vs  project models.py files")
    lines.append("=" * 70)
    lines.append("")

    # ── Summary ───────────────────────────────────────────────────────────────
    missing_from_models = [f for f in result.field_issues if f[2] == 'MISSING_FROM_MODELS']
    type_mismatches     = [f for f in result.field_issues if f[2] == 'TYPE_MISMATCH']
    orphaned_fields     = [f for f in result.field_issues if f[2] == 'IN_MODELS_NOT_IN_DB']

    lines.append("SUMMARY")
    lines.append("-" * 40)
    lines.append(f"  Tables in DB, no matching model    : {len(result.in_db_not_in_models)}")
    lines.append(f"  Models with no matching DB table   : {len(result.in_models_not_in_db)}")
    lines.append(f"  Fields missing from models.py      : {len(missing_from_models)}")
    lines.append(f"  Field type mismatches              : {len(type_mismatches)}")
    lines.append(f"  Fields in models.py but not in DB  : {len(orphaned_fields)}")
    lines.append("")

    total_issues = (
        len(result.in_db_not_in_models) +
        len(result.in_models_not_in_db) +
        len(missing_from_models) +
        len(type_mismatches) +
        len(orphaned_fields)
    )
    if total_issues == 0:
        lines.append("  ✓ No issues found — models.py is in sync with the DB.")
        lines.append("")
        return '\n'.join(lines)

    # ── Section A: Tables in DB with no model ─────────────────────────────────
    if result.in_db_not_in_models:
        lines.append("=" * 70)
        lines.append("A. DB TABLES WITH NO MATCHING MODEL")
        lines.append("   These tables exist in the DB but no Django model was found.")
        lines.append("   Action: add a model class, or confirm they are intentionally unmanaged.")
        lines.append("=" * 70)
        for db_table, idb_model in sorted(result.in_db_not_in_models):
            lines.append(f"\n  Table: {db_table}")
            lines.append(f"  Columns ({len(idb_model.fields)}):")
            for fname, fdef in idb_model.fields.items():
                lines.append(f"    {fname:<45} {fdef.field_type}")
        lines.append("")

    # ── Section B: Models with no DB table ────────────────────────────────────
    if result.in_models_not_in_db:
        lines.append("=" * 70)
        lines.append("B. MODELS WITH NO MATCHING DB TABLE")
        lines.append("   These model classes exist in models.py but the table was not")
        lines.append("   found in the DB via inspectdb.")
        lines.append("   Action: check if the migration was applied, or if the model")
        lines.append("   was removed from the DB but not from models.py.")
        lines.append("=" * 70)
        for model_name, source_file, db_table in sorted(result.in_models_not_in_db):
            lines.append(f"\n  Model    : {model_name}")
            lines.append(f"  Expected : {db_table}")
            lines.append(f"  File     : {source_file}")
        lines.append("")

    # ── Section C: Fields missing from models.py ──────────────────────────────
    if missing_from_models:
        lines.append("=" * 70)
        lines.append("C. FIELDS IN DB BUT MISSING FROM MODELS.PY")
        lines.append("   These columns exist in the DB (added by migrations) but are")
        lines.append("   not declared in the corresponding model class.")
        lines.append("   Action: add the field to models.py. No new migration needed")
        lines.append("   since the column already exists in the DB — but run")
        lines.append("   makemigrations --check afterwards to confirm Django agrees.")
        lines.append("=" * 70)

        # Group by table for readability
        by_table = defaultdict(list)
        for table, fname, _, detail in missing_from_models:
            by_table[table].append((fname, detail))

        for table in sorted(by_table.keys()):
            lines.append(f"\n  Table: {table}")
            for fname, detail in sorted(by_table[table]):
                lines.append(f"    ✗  {fname:<43} {detail}")
        lines.append("")

    # ── Section D: Type mismatches ────────────────────────────────────────────
    if type_mismatches:
        lines.append("=" * 70)
        lines.append("D. FIELD TYPE MISMATCHES")
        lines.append("   The field exists in both DB and models.py but the types differ.")
        lines.append("   This may be harmless (e.g. CharField vs TextField both map to")
        lines.append("   'text' in Postgres) or may indicate a real schema problem.")
        lines.append("   Action: review each case — update models.py if the DB type is")
        lines.append("   correct, or write a migration if models.py should change the DB.")
        lines.append("=" * 70)

        by_table = defaultdict(list)
        for table, fname, _, detail in type_mismatches:
            by_table[table].append((fname, detail))

        for table in sorted(by_table.keys()):
            lines.append(f"\n  Table: {table}")
            for fname, detail in sorted(by_table[table]):
                lines.append(f"    ~  {fname:<43} {detail}")
        lines.append("")

    # ── Section E: Fields in models.py not in DB ──────────────────────────────
    if orphaned_fields:
        lines.append("=" * 70)
        lines.append("E. FIELDS IN MODELS.PY BUT NOT IN DB")
        lines.append("   These fields are declared in models.py but the column was not")
        lines.append("   found in the DB by inspectdb.")
        lines.append("   Possible causes:")
        lines.append("     - Migration was written but never applied")
        lines.append("     - Column was dropped from DB but not removed from models.py")
        lines.append("     - Field uses a custom db_column= that differs from the attribute name")
        lines.append("   Action: check migration history, then either apply the migration,")
        lines.append("   remove the field from models.py, or add a db_column= override.")
        lines.append("=" * 70)

        by_table = defaultdict(list)
        for table, fname, _, detail in orphaned_fields:
            by_table[table].append((fname, detail))

        for table in sorted(by_table.keys()):
            lines.append(f"\n  Table: {table}")
            for fname, detail in sorted(by_table[table]):
                lines.append(f"    ?  {fname:<43} {detail}")
        lines.append("")

    # ── Recommended next steps ────────────────────────────────────────────────
    lines.append("=" * 70)
    lines.append("RECOMMENDED NEXT STEPS")
    lines.append("=" * 70)
    lines.append("")
    if missing_from_models:
        lines.append("  1. For each entry in Section C:")
        lines.append("     - Add the missing field to the relevant models.py")
        lines.append("     - Use the inspectdb output as the type reference")
        lines.append("     - Preserve all existing custom methods and Meta options")
        lines.append("")
    if type_mismatches:
        lines.append("  2. For each entry in Section D:")
        lines.append("     - Review whether the mismatch matters for your DB")
        lines.append("       (text vs varchar is usually fine; uuid vs integer is not)")
        lines.append("     - Update models.py or write a migration as appropriate")
        lines.append("")
    if orphaned_fields:
        lines.append("  3. For each entry in Section E:")
        lines.append("     - Run: docker compose exec backend python manage.py showmigrations")
        lines.append("     - Check if the migration adding this field was applied")
        lines.append("     - If not applied: python manage.py migrate")
        lines.append("     - If column was intentionally dropped: remove from models.py")
        lines.append("")
    lines.append("  After all models.py updates:")
    lines.append("  4. docker compose exec backend python manage.py makemigrations --check")
    lines.append("     (should exit clean with no changes detected)")
    lines.append("  5. docker compose exec backend python manage.py migrate --check")
    lines.append("     (should exit clean — DB matches migration history)")
    lines.append("  6. python3 verify_structure.py seed_fixed.sql && \\")
    lines.append("     docker compose exec -T db psql -U django -d django_backend \\")
    lines.append("       < verify_structure.sql > structure_report.txt 2>&1")
    lines.append("     (full structural audit to confirm everything is in sync)")
    lines.append("")

    return '\n'.join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) != 3:
        print("Usage: python3 diff_models.py inspectdb_output.py path/to/apps/")
        print("")
        print("Step 1 — generate inspectdb output:")
        print("  docker compose exec backend python manage.py inspectdb > inspectdb_output.py")
        print("")
        print("Step 2 — run the diff:")
        print("  python3 diff_models.py inspectdb_output.py django_backend/apps/")
        sys.exit(1)

    inspectdb_file = sys.argv[1]
    apps_root      = sys.argv[2]

    if not os.path.exists(inspectdb_file):
        print(f"Error: inspectdb file not found: {inspectdb_file}")
        sys.exit(1)

    if not os.path.isdir(apps_root):
        print(f"Error: apps directory not found: {apps_root}")
        sys.exit(1)

    # ── Parse inspectdb output ────────────────────────────────────────────────
    print(f"Parsing inspectdb output: {inspectdb_file}")
    inspectdb_models = parse_inspectdb(inspectdb_file)
    print(f"  Found {len(inspectdb_models)} models in inspectdb output")

    # ── Parse all project models.py files ─────────────────────────────────────
    models_files = find_models_files(apps_root)
    print(f"\nScanning apps directory: {apps_root}")
    print(f"  Found {len(models_files)} models.py file(s):")

    project_models = {}
    for mfile in models_files:
        rel = os.path.relpath(mfile, apps_root)
        parsed = parse_models_file(mfile)
        print(f"    {rel:<60} {len(parsed)} model(s)")
        project_models.update(parsed)

    print(f"\n  Total project models: {len(project_models)}")

    # ── Run diff ──────────────────────────────────────────────────────────────
    print("\nRunning diff ...")
    result = diff(inspectdb_models, project_models)

    # ── Format and output report ───────────────────────────────────────────────
    report = format_report(result, inspectdb_models, project_models)

    out_file = 'diff_models_report.txt'
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(report)

    print(report)
    print(f"Report also saved to: {out_file}")


if __name__ == '__main__':
    main()
