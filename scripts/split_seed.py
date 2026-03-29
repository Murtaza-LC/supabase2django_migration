"""
Author: Murtaza Nuruddin

split_seed.py

Splits a SQL seed file into smaller load-order-based seed files.

The script scans `INSERT INTO public.<table> ... ON CONFLICT ...;` statements,
skips tables and rows listed in `SKIP_TABLES` and `SKIP_IDS`, groups remaining
statements by `TABLE_GROUPS`, and writes one SQL file per group with BEGIN/COMMIT
wrappers. Statements that are skipped are written to `seed_SKIPPED.sql` for review.

Generates:
  seed_01_prereq1.sql
  seed_02_prereq2.sql
  seed_03_requirements.sql
  seed_04_X.sql
  seed_05_X2.sql
  seed_06_everything_else.sql
  seed_SKIPPED.sql              ← skipped rows for reference

Usage:
    python3 split_seed.py seed_fixed.sql

Notes:
- Tables not listed in `TABLE_GROUPS` are placed in Group 6 by default
- Only INSERT statements matching the expected regex pattern are processed
- Update `SKIP_TABLES`, `SKIP_IDS`, `TABLE_GROUPS`, `GROUP_FILENAMES`, and
  `GROUP_HEADERS` to fit your dataset and load order
- The script prints row counts, skipped counts, and the recommended load order

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

# Load order groups — table → group number
# UPDATE WITH YOUR SPECIFIC DATA
TABLE_GROUPS = {
    # Group 1: App1
    'Table1': 1,

    # Group 2: App2
    'Table2': 2,

    # Group 3: App3
    'Table3': 3,

    # Group 4: X
    'Table4': 4,
    'Table5': 4,
    'Table6': 4,

    # Group 5: X2
    'Table7': 5,
    'Table8': 5,
    'Table9': 5,
}

GROUP_FILENAMES = {
    1: 'seed_01_prereq.sql',
    2: 'seed_02_prereq2.sqlsql',
    3: 'seed_03_requirements.sql',
    4: 'seed_03_X.sql',
    5: 'seed_03_X2.sql',
    6: 'seed_06_everything_else.sql',
}

GROUP_HEADERS = {
    1: '-- Group 1: APP1\n',
    2: '-- Group 2: APP2\n',
    3: '-- Group 3: APP3\n',
    4: '-- Group 4: X detsils\n',
    5: '-- Group 5: X2 details\n',
    6: '-- Group 6: Everything else\n',
}


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 split_seed.py seed_fixed.sql")
        sys.exit(1)

    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        content = f.read()

    # Split into individual INSERT statements (preserve the ON CONFLICT clause)
    insert_re = re.compile(
        r'(INSERT INTO public\.(\w+)\s*\([^)]+\)\s*VALUES\s*\(.+?\)\s*ON CONFLICT[^\n]*;)',
        re.DOTALL
    )

    groups = defaultdict(list)       # group_num -> [sql_statement, ...]
    skipped = []
    counts = defaultdict(int)
    skipped_counts = defaultdict(int)

    # Also capture any non-INSERT lines (comments, SET commands etc.)
    # Split content into tokens: INSERT statements and everything else
    last_end = 0
    preamble = []

    for m in insert_re.finditer(content):
        # Capture any preamble/comment lines before this INSERT
        gap = content[last_end:m.start()].strip()
        if gap and last_end == 0:
            preamble.append(gap)
        last_end = m.end()

        stmt  = m.group(1)
        table = m.group(2)

        if table in SKIP_TABLES:
            skipped.append(stmt)
            skipped_counts[table] += 1
            continue

        # Also skip individual rows in otherwise-valid tables if they
        # reference an SKIP UUID in any FK column
        if any(uid in stmt.lower() for uid in SKIP_IDS):
            skipped.append(stmt)
            skipped_counts[table] += 1
            continue

        group = TABLE_GROUPS.get(table, 6)
        groups[group].append(stmt)
        counts[table] += 1

    # Write group files
    for group_num, statements in sorted(groups.items()):
        filename = GROUP_FILENAMES[group_num]
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(GROUP_HEADERS[group_num])
            f.write(f'-- {len(statements)} INSERT statements\n\n')
            f.write('BEGIN;\n\n')
            for stmt in statements:
                f.write(stmt + '\n\n')
            f.write('COMMIT;\n')
        print(f"  Wrote {filename}  ({len(statements)} rows)")

    # Write skipped file for reference
    if skipped:
        with open('seed_SKIPPED.sql', 'w', encoding='utf-8') as f:
            f.write('-- SKIPPED rows — reference SKIP UUIDs or archived tables\n')
            f.write('-- DO NOT LOAD these\n\n')
            for stmt in skipped:
                f.write(stmt + '\n\n')
        print(f"  Wrote seed_SKIPPED.sql  ({len(skipped)} rows — DO NOT LOAD)")

    print()
    print("=" * 50)
    print("Row counts by table:")
    for table, count in sorted(counts.items()):
        print(f"  {table:<45} {count:>5}")
    print()
    print("Skipped tables:")
    for table, count in sorted(skipped_counts.items()):
        print(f"  {table:<45} {count:>5}  (SKIP refs)")

    print()
    print("Load order:")
    print("  docker compose exec -T db psql -U django -d django_backend -v ON_ERROR_STOP=1 < seed_01_prereq.sql")
    print("  docker compose exec -T db psql -U django -d django_backend -v ON_ERROR_STOP=1 < seed_02_prereq2.sql")
    print("  docker compose exec -T db psql -U django -d django_backend -v ON_ERROR_STOP=1 < seed_03_requirements.sql")
    print("  docker compose exec -T db psql -U django -d django_backend -v ON_ERROR_STOP=1 < seed_04_X.sql")
    print("  docker compose exec -T db psql -U django -d django_backend -v ON_ERROR_STOP=1 < seed_05_X2.sql")
    print("  docker compose exec -T db psql -U django -d django_backend -v ON_ERROR_STOP=1 < seed_06_everything_else.sql")


if __name__ == '__main__':
    main()
