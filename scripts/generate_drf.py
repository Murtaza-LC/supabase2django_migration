"""
Author: Murtaza Nuruddin

Filename: generate_drf.py
Auto-generates Django REST Framework API scaffolding for apps in the `apps/` directory.

What it does:
- Scans each app for models defined in `models.py`
- Skips sensitive models such as User / Users / AuthUsers / AccountUser
- Creates or overwrites:
  - serializers.py
  - views.py
  - urls.py

Generated APIs use:
- `TenantAwareModelViewSet` for tenant-safe access patterns
- `IsAuthenticated` for authenticated-only endpoints
- DRF router-based URLs with model names converted to kebab-case plural routes

Notes:
- This script overwrites existing serializers.py, views.py, and urls.py files
- Review generated code before using in production
- Ensure `apps.core.views.TenantAwareModelViewSet` exists before running

Usage:
    python generate_drf_files.py
"""
import os
import re

APPS_DIR = "apps"

def camel_to_kebab(name):
    """Converts CamelCase model names to kebab-case for RESTful URLs."""
    s1 = re.sub('(.)([A-Z][a-z]+)', r'\1-\2', name)
    kebab = re.sub('([a-z0-9])([A-Z])', r'\1-\2', s1).lower()
    return kebab if kebab.endswith('s') else f"{kebab}s"

def generate_drf_files():
    if not os.path.exists(APPS_DIR):
        print(f"❌ Apps directory '{APPS_DIR}' not found.")
        return

    app_folders = [f for f in os.listdir(APPS_DIR) if os.path.isdir(os.path.join(APPS_DIR, f))]
    
    # SECURITY: Define models that should NEVER be exposed via generic ViewSets
    SENSITIVE_MODELS = ['Users', 'AuthUsers', 'User', 'AccountUser']

    total_models_exposed = 0

    for app_name in app_folders:
        app_path = os.path.join(APPS_DIR, app_name)
        models_path = os.path.join(app_path, "models.py")

        if not os.path.exists(models_path):
            continue

        with open(models_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        all_models = re.findall(r'class\s+([A-Za-z0-9_]+)\(models\.Model\):', content)
        
        # FILTER: Keep only the safe models for API generation
        safe_models = [m for m in all_models if m not in SENSITIVE_MODELS]
        
        if not safe_models:
            continue

        total_models_exposed += len(safe_models)
        print(f"⚙️ Generating API for {app_name} ({len(safe_models)} models exposed, {len(all_models) - len(safe_models)} hidden)...")

        # --- 1. Generate serializers.py ---
        serializers_content = "from rest_framework import serializers\n"
        serializers_content += f"from .models import {', '.join(safe_models)}\n\n"
        
        for model in safe_models:
            serializers_content += f"class {model}Serializer(serializers.ModelSerializer):\n"
            serializers_content += f"    class Meta:\n"
            serializers_content += f"        model = {model}\n"
            serializers_content += f"        fields = '__all__'\n\n"

        with open(os.path.join(app_path, "serializers.py"), 'w', encoding='utf-8') as f:
            f.write(serializers_content)

        # --- 2. Generate views.py (SECURE TENANT VERSION) ---
        views_content = "from rest_framework import viewsets\n"
        views_content += "from rest_framework.permissions import IsAuthenticated\n"
        # Import the custom Base ViewSet that handles Row Level Security
        views_content += "from apps.core.views import TenantAwareModelViewSet\n"
        views_content += f"from .models import {', '.join(safe_models)}\n"
        views_content += f"from .serializers import {', '.join([m + 'Serializer' for m in safe_models])}\n\n"

        for model in safe_models:
            # Notice we use TenantAwareModelViewSet here instead of viewsets.ModelViewSet
            views_content += f"class {model}ViewSet(TenantAwareModelViewSet):\n"
            views_content += f"    queryset = {model}.objects.all()\n"
            views_content += f"    serializer_class = {model}Serializer\n"
            views_content += f"    model = {model}\n" 
            views_content += f"    permission_classes = [IsAuthenticated]\n\n"

        with open(os.path.join(app_path, "views.py"), 'w', encoding='utf-8') as f:
            f.write(views_content)

        # --- 3. Generate urls.py ---
        urls_content = "from django.urls import path, include\n"
        urls_content += "from rest_framework.routers import DefaultRouter\n"
        urls_content += f"from .views import {', '.join([m + 'ViewSet' for m in safe_models])}\n\n"
        
        urls_content += "router = DefaultRouter()\n"
        for model in safe_models:
            route_name = camel_to_kebab(model)
            urls_content += f"router.register(r'{route_name}', {model}ViewSet)\n"
        
        urls_content += "\nurlpatterns = [\n"
        urls_content += "    path('', include(router.urls)),\n"
        urls_content += "]\n"

        with open(os.path.join(app_path, "urls.py"), 'w', encoding='utf-8') as f:
            f.write(urls_content)

    print(f"\n✅ Successfully generated Secure API structure for {total_models_exposed} models.")
    print(f"🔒 Sensitive models ({', '.join(SENSITIVE_MODELS)}) were skipped for security.")
    print(f"⚠️  Don't forget to create x_project/views.py with TenantAwareModelViewSet!")

if __name__ == "__main__":
    generate_drf_files()
