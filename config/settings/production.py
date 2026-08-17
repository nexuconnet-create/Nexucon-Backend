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
    # Ensure postgis engine is used
    DATABASES['default']['ENGINE'] = 'django.contrib.gis.db.backends.postgis'

# Static files (WhiteNoise)
MIDDLEWARE.insert(1, 'whitenoise.middleware.WhiteNoiseMiddleware')
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# Celery
if os.getenv("CELERY_BROKER_URL"):
    CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL")

# Allowed Hosts - ensure Render domain is included
# The render.yaml sets DJANGO_ALLOWED_HOSTS to nexucon-backend.onrender.com
# ALLOWED_HOSTS is loaded from base.py via the DJANGO_ALLOWED_HOSTS env var

# CORS and CSRF for Vercel
if os.getenv("FRONTEND_URL"):
    frontend_url = os.getenv("FRONTEND_URL")
    CORS_ALLOWED_ORIGINS.append(frontend_url)
    CSRF_TRUSTED_ORIGINS = [frontend_url]

# Allow any Vercel domain dynamically to support preview deployments
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https:\/\/.*\.vercel\.app$",
]
CSRF_TRUSTED_ORIGINS.append("https://*.vercel.app")

# Alternatively, allow all if explicitly set (useful for initial Vercel setup)
if os.getenv("CORS_ALLOW_ALL_ORIGINS", "False") == "True":
    CORS_ALLOW_ALL_ORIGINS = True
