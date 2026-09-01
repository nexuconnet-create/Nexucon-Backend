from .base import *
import dj_database_url
import os

DEBUG = os.getenv("DEBUG", "False") == "True"

# Security
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = 'DENY'

# Database
# If DATABASE_URL is set, use dj_database_url to parse it
if os.getenv("DATABASE_URL"):
    DATABASES['default'] = dj_database_url.config(
        default=os.getenv("DATABASE_URL"),
        conn_max_age=600,
        conn_health_checks=True,
    )
    # Set engine to postgis only if ENABLE_GIS is enabled or requested
    if os.getenv("ENABLE_GIS", "False").lower() in ("true", "1") or os.getenv("DATABASE_ENGINE") == "django.contrib.gis.db.backends.postgis":
        DATABASES['default']['ENGINE'] = 'django.contrib.gis.db.backends.postgis'
    else:
        DATABASES['default']['ENGINE'] = 'django.db.backends.postgresql'

# Static files (WhiteNoise)
MIDDLEWARE.insert(1, 'whitenoise.middleware.WhiteNoiseMiddleware')
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STORAGES["staticfiles"] = {
    "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
}

# Celery
if os.getenv("CELERY_BROKER_URL"):
    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL")

# Allowed Hosts - ensure Render domain is included
# The render.yaml sets DJANGO_ALLOWED_HOSTS to nexucon-backend.onrender.com
# ALLOWED_HOSTS is loaded from base.py via the DJANGO_ALLOWED_HOSTS env var

# CORS and CSRF for Vercel
try:
    CSRF_TRUSTED_ORIGINS = list(CSRF_TRUSTED_ORIGINS)
except NameError:
    CSRF_TRUSTED_ORIGINS = []

try:
    CORS_ALLOWED_ORIGINS = list(CORS_ALLOWED_ORIGINS)
except NameError:
    CORS_ALLOWED_ORIGINS = []

if os.getenv("FRONTEND_URL"):
    frontend_raw = os.getenv("FRONTEND_URL", "").strip()
    for item in frontend_raw.split(","):
        cleaned = item.strip().rstrip("/")
        if cleaned:
            if cleaned not in CORS_ALLOWED_ORIGINS:
                CORS_ALLOWED_ORIGINS.append(cleaned)
            if cleaned not in CSRF_TRUSTED_ORIGINS:
                CSRF_TRUSTED_ORIGINS.append(cleaned)

def _sanitize_origin(origin):
    origin = origin.strip().rstrip("/")
    if "://" in origin:
        parts = origin.split("://", 1)
        scheme = parts[0]
        rest = parts[1].split("/", 1)[0]
        return f"{scheme}://{rest}"
    return origin

def _sanitize_csrf_origin(origin):
    origin = origin.strip().rstrip("/")
    if origin.startswith("https://*.") or origin.startswith("http://*."):
        return origin
    if "://" in origin:
        parts = origin.split("://", 1)
        scheme = parts[0]
        rest = parts[1].split("/", 1)[0]
        return f"{scheme}://{rest}"
    return origin

CORS_ALLOWED_ORIGINS = list(dict.fromkeys([_sanitize_origin(o) for o in CORS_ALLOWED_ORIGINS if o]))
CSRF_TRUSTED_ORIGINS = list(dict.fromkeys([_sanitize_csrf_origin(o) for o in CSRF_TRUSTED_ORIGINS if o]))

# Allow any Vercel domain dynamically to support preview deployments
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https:\/\/.*\.vercel\.app$",
]
if "https://*.vercel.app" not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append("https://*.vercel.app")

# Cross-Origin Cookie Settings for Vercel -> Render communication
SESSION_COOKIE_SAMESITE = 'None'
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SAMESITE = 'None'
CSRF_COOKIE_SECURE = True

# Update SIMPLE_JWT settings for cross-origin cookies
if 'SIMPLE_JWT' in locals():
    SIMPLE_JWT['AUTH_COOKIE_SAMESITE'] = 'None'
    SIMPLE_JWT['AUTH_COOKIE_SECURE'] = True

# Alternatively, allow all if explicitly set (useful for initial Vercel setup)
if os.getenv("CORS_ALLOW_ALL_ORIGINS", "False") == "True":
    CORS_ALLOW_ALL_ORIGINS = True
