"""
Author: Murtaza Nuruddin

verify_seed.py

Generates a SQL verification script for a seed file.

The script parses INSERT statements from a seed SQL file, excludes rows and
tables listed in `SKIP_TABLES` and `SKIP_IDS`, counts the expected rows per
table, and writes `verify_seed.sql` to compare expected counts against actual
database counts.

Usage:
    python3 verify_seed.py seed_fixed.sql

Notes:
- Compares row counts in the seed file against what's actually in the DB.
- Only INSERT statements matching the expected regex pattern are counted
- Skipped rows are excluded from expected totals
- The generated SQL reports expected, actual, gap, and load status per table
- The script also prints a summary and generates a ready-to-run SQL verification query.
"""

import re
import sys
from collections import defaultdict

# Tables to skip entirely — for instance if they ref content that no longer exists
SKIP_TABLES = {
    'table1',
    'table2',
}

# Individual rows referencing any of these UUIDs are also skipped,
# even in tables that are otherwise valid 
SKIP_IDS = {
    "061be30a-ae19-4a23-a446-35be5851444f","0a098365-264e-4bcd-bd4a-35cdba74b087f",
    "0a6feeb0-fde3-410c-98e1-79c236b654ge","0e892742-a332-4b16-9f7f-ab47cc2a389g2",
    "fbb3a4fa-bbcb-46d3-7896-7b34cdf5959c",
}

def main():
    if len(sys.argv) != 2:
        print("Usage: python3 verify_seed.py seed_fixed.sql")
        sys.exit(1)

    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        content = f.read()

    insert_re = re.compile(
        r'INSERT INTO public\.(\w+)\s*\([^)]+\)\s*VALUES\s*\(.+?\)\s*ON CONFLICT[^\n]*;',
        re.DOTALL
    )

    # Count expected rows per table (excluding skipped)
    expected = defaultdict(int)
    skipped  = defaultdict(int)

    for m in insert_re.finditer(content):
        table = m.group(0).split('(')[0].split('.')[-1].strip()
        # re-extract table name cleanly
        t = re.match(r'INSERT INTO public\.(\w+)', m.group(0))
        if not t:
            continue
        table = t.group(1)

        if table in SKIP_TABLES:
            skipped[table] += 1
            continue
        if any(uid in m.group(0).lower() for uid in SKIP_IDS):
            skipped[table] += 1
            continue
        expected[table] += 1

    # Build verification SQL
    lines = [
        "-- verify_seed.sql",
        "-- Compares expected row counts (from seed file) vs actual DB counts.",
        "-- 'gap' column shows how many rows are missing (0 = fully loaded).\n",
        "SELECT",
        "    table_name,",
        "    expected,",
        "    actual,",
        "    expected - actual AS gap,",
        "    CASE WHEN expected = actual THEN '✓ OK'",
        "         WHEN actual = 0        THEN '✗ EMPTY'",
        "         WHEN actual < expected THEN '⚠ PARTIAL'",
        "         ELSE '? EXTRA' END AS status",
        "FROM (",
        "    VALUES",
    ]

    rows = []
    for table, count in sorted(expected.items()):
        rows.append(
            f"        ('{table}', {count}, "
            f"(SELECT COUNT(*) FROM {table}))"
        )
    lines.append(',\n'.join(rows))
    lines.append(
        ") AS t(table_name, expected, actual)\n"
        "ORDER BY gap DESC, table_name;"
    )

    lines.append("\n-- Skipped tables (intentionally not loaded):")
    for table, count in sorted(skipped.items()):
        lines.append(f"-- {table}: {count} rows skipped (SKIP refs or archived)")

    sql = '\n'.join(lines)

    with open('verify_seed.sql', 'w') as f:
        f.write(sql)

    # Print summary to terminal
    total_expected = sum(expected.values())
    total_skipped  = sum(skipped.values())
    print(f"{'Table':<45} {'Expected':>10}")
    print("-" * 57)
    for table, count in sorted(expected.items()):
        print(f"  {table:<43} {count:>10}")
    print("-" * 57)
    print(f"  {'TOTAL (to be loaded)':<43} {total_expected:>10}")
    print(f"  {'SKIPPED (SKIP / archived)':<43} {total_skipped:>10}")
    print()
    print("Wrote verify_seed.sql")
    print("Run:")
    print("  docker compose exec -T db psql -U django -d django_backend < verify_seed.sql")


if __name__ == '__main__':
    main()
