"""
Author: Murtaza Nuruddin

splitmodels.py

Utilities for splitting an inspectdb-generated Django model file into multiple
domain-oriented Django apps.

This script reads a single ``master_models.py`` file, identifies each model's
database table via ``Meta.db_table``, maps that table to a target Django app,
rewrites cross-model relationships to use string-based app-qualified references,
and writes the models into per-app ``models.py`` files under the configured
``APPS_DIR``.

It also:
- creates missing Django apps via ``manage.py startapp``
- updates each generated ``apps.py`` with the correct dotted app path
- enables model management by replacing ``managed = False`` with ``managed = True``
- generates helper outputs for ``INSTALLED_APPS`` registration and migration review

Expected workflow:
1. Run ``inspectdb`` and save the output as ``master_models.py``.
2. Configure table-prefix-to-app mappings in ``get_unified_app()``.
3. Run this script from the Django project root.
4. Review:
   - ``apps_to_register.txt``
   - ``migration_report.log``
   - generated app ``models.py`` files

Important notes:
- The script assumes models inherit from ``models.Model``.
- The script relies on ``Meta.db_table`` to determine routing.
- Relationship rewriting currently supports ``ForeignKey``,
  ``ManyToManyField``, and ``OneToOneField``.
- Table prefix mappings should be reviewed carefully before use in production.
"""

import os
import re
import subprocess
import datetime
from collections import defaultdict

# --- CONFIGURATION ---
INPUT_FILE = "master_models.py"  # The result of your inspectdb
APPS_DIR = "apps"                # Target: django_backend/apps
current_dir = os.getcwd()
ENV_VARS = {
    **os.environ,
    "PYTHONPATH": f"{current_dir}:{os.environ.get('PYTHONPATH', '')}",
    "DJANGO_SETTINGS_MODULE": "settings.local" 
}

def get_unified_app(table_name):
    """Groups 82+ micro-prefixes into ~10 logical business domains."""
    # Priority 1: Exact match for the core user and datasets table
    if table_name == 'users':
        return 'x_auth'
    if table_name == 'feature1':
        return 'x_engine'

    # Priority 2: Domain Mapping
    mapping = {
        # INFO & Model Domain
        ('domain_', 'feature1_', 'feature2_', 'feature3_', 'value_', '...'): 'app_1',
        # Next Domain
        ('domain2_', 'feature1_', 'feature2_', 'feature3_', 'value_', '...'): 'app_2',
        # Next Domain
        ('domain2_', 'feature1_', 'feature2_', 'feature3_', 'value_', '...'): 'app_3',
        # Add Others as needed
    }

    for prefixes, target_app in mapping.items():
        if table_name.startswith(prefixes):
            return target_app
            
    return 'core_engine'

def fix_app_config(app_name):
    """Automatically updates apps.py to use the correct full import path and adds verbose_name."""
    apps_py_path = os.path.join(APPS_DIR, app_name, "apps.py")
    if os.path.exists(apps_py_path):
        with open(apps_py_path, 'r') as f:
            content = f.read()
        
        # Create a beautiful display name (e.g., 'app1_xx' -> 'app2_xx')
        pretty_name = app_name.replace('_', ' ').title()
        
        # Quick polish for acronyms (e.g., 'App_xxyy' -> 'App Xxyy')
        pretty_name = pretty_name.replace('Pattern1 ', 'PATTERN ')
        if pretty_name == 'X Auth':
            pretty_name = 'Authentication & Identity'
        
        # Replace name and inject verbose_name immediately after it with proper indentation
        updated_content = re.sub(
            rf"name\s*=\s*['\"]{app_name}['\"]",
            f"name = 'apps.{app_name}'\n    verbose_name = '{pretty_name}'",
            content
        )
        
        with open(apps_py_path, 'w') as f:
            f.write(updated_content)
        return True
    return False


def run_startapp(app_name):
    """Executes startapp and ensures directory exists."""
    target_path = os.path.join(APPS_DIR, app_name)
    if not os.path.exists(target_path):
        print(f"🏗️  Creating unified app: {app_name}...")
        os.makedirs(target_path, exist_ok=True)
        try:
            subprocess.run(
                ["python", "manage.py", "startapp", app_name, target_path],
                env=ENV_VARS, check=True, capture_output=True
            )
            # --- NEW STEP: Fix the apps.py immediately after creation ---
            fix_app_config(app_name)
            return True
        except subprocess.CalledProcessError as e:
            print(f"❌ Failed to create app {app_name}: {e.stderr.decode()}")
            return False
    return True 

def auto_migrate_models():
    log_entries = [f"=== Unified Migration Report - {datetime.datetime.now()} ===\n"]
    os.makedirs(APPS_DIR, exist_ok=True)
    
    if not os.path.exists(INPUT_FILE):
        print(f"❌ Error: {INPUT_FILE} not found. Run inspectdb first.")
        return

    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        content = f.read()

    class_pattern = re.compile(r'(class\s+([A-Za-z0-9_]+)\(models\.Model\):.*?)(?=\nclass\s|\Z)', re.DOTALL)
    
    # PASS 1: Build Global Registry
    model_registry = {}
    for match in class_pattern.finditer(content):
        class_block, model_name = match.group(1), match.group(2)
        db_table_match = re.search(r"db_table\s*=\s*'([a-z0-9_]+)'", class_block)
        table_name = db_table_match.group(1) if db_table_match else "unassigned"
        model_registry[model_name] = get_unified_app(table_name)

    # PASS 2: Transform and Route
    apps_dict = defaultdict(list)
    for match in class_pattern.finditer(content):
        class_block, model_name = match.group(1), match.group(2)
        current_app = model_registry[model_name]

        def rel_replacer(m):
            field_type, target_model = m.group(1), m.group(2)
            if target_model == 'self':
                return f"models.{field_type}('self',"
            target_app = model_registry.get(target_model, current_app)
            return f"models.{field_type}('{target_app}.{target_model}',"

        # UPDATED REGEX: Now catches ForeignKey, ManyToManyField, AND OneToOneField
        class_block = re.sub(
            r"models\.(ForeignKey|ManyToManyField|OneToOneField)\(\'?([A-Za-z0-9_]+)\'?\s*,", 
            rel_replacer, 
            class_block
        )
        
        class_block = class_block.replace("managed = False", "managed = True")
        apps_dict[current_app].append(class_block)
        log_entries.append(f"[ROUTE] {model_name} -> {current_app}")

    # Step 3: Create and Write
    created_apps = sorted(apps_dict.keys())
    for app_name in created_apps:
        if run_startapp(app_name):
            model_path = os.path.join(APPS_DIR, app_name, "models.py")
            with open(model_path, 'w', encoding='utf-8') as f:
                f.write("from django.db import models\n\n")
                f.write("\n\n".join(apps_dict[app_name]))
            log_entries.append(f"[WRITE] Updated {model_path}")

    # Output Helpers
    reg_list = [f"    'apps.{app}'," for app in created_apps]
    with open("apps_to_register.txt", 'w') as f:
        f.write("# Paste into INSTALLED_APPS in settings/local.py\n\n")
        f.write("\n".join(reg_list))

    with open("migration_report.log", 'w') as f:
        f.write("\n".join(log_entries))
        
    print(f"\n✅ Created {len(created_apps)} apps in {APPS_DIR}")
    print(f"📋 Check apps_to_register.txt and migration_report.log")

if __name__ == "__main__":
    auto_migrate_models()
