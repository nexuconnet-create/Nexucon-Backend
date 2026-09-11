import os
from pathlib import Path
from datetime import timedelta

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Zero-dependency .env loader
env_file = BASE_DIR / '.env'
if env_file.exists():
    try:
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k not in os.environ:
                        os.environ[k] = v
    except Exception:
        pass

DEBUG = os.getenv("DJANGO_DEBUG", "True") == "True"
# Dev-only fallback secret. Production (settings/production.py) refuses to
# boot without DJANGO_SECRET_KEY — a real key is never committed.
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dummy-secret-key-for-dev-only" if DEBUG else "")
if not SECRET_KEY and not DEBUG:
    raise RuntimeError("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG=False.")
ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_ALL_HEADERS = True
CORS_ALLOW_METHODS = ["DELETE", "GET", "OPTIONS", "PATCH", "POST", "PUT"]

CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "https://api.nexucon.net",
    "http://api.nexucon.net",
    "https://nexucon.net",
    "http://nexucon.net",
    "http://187.7.20.123",
    "http://187.7.20.123:8000",
    "https://nexucon-backend.onrender.com",
    "https://nexucon-frontend-8x3a.vercel.app",
    "https://www.nexucon.net",
    "https://187.7.20.123",
]
_extra_cors = os.getenv("CORS_ALLOWED_ORIGINS", "") or os.getenv("DJANGO_CORS_ALLOWED_ORIGINS", "")
if _extra_cors:
    for _orig in _extra_cors.split(","):
        _orig = _orig.strip()
        if _orig and _orig not in CORS_ALLOWED_ORIGINS:
            CORS_ALLOWED_ORIGINS.append(_orig)

CSRF_TRUSTED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "https://api.nexucon.net",
    "http://api.nexucon.net",
    "https://nexucon.net",
    "http://nexucon.net",
    "http://187.7.20.123",
    "http://187.7.20.123:8000",
    "https://*.vercel.app",
    "https://nexucon-backend.onrender.com",
    "https://www.nexucon.net",
    "https://187.7.20.123",
]
_extra_csrf = os.getenv("CSRF_TRUSTED_ORIGINS", "") or os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "")
if _extra_csrf:
    for _orig in _extra_csrf.split(","):
        _orig = _orig.strip()
        if _orig and _orig not in CSRF_TRUSTED_ORIGINS:
            CSRF_TRUSTED_ORIGINS.append(_orig)


# Dynamically add any env-defined origins ensuring proper schemes
for _env_key in ("FRONTEND_URL", "NEXT_PUBLIC_API_URL", "CSRF_TRUSTED_ORIGINS"):
    _val = os.getenv(_env_key, "").strip()
    if _val:
        for _item in _val.split(","):
            _cleaned = _item.strip().rstrip("/")
            if _cleaned and _cleaned != "*":
                if _cleaned.startswith("http://") or _cleaned.startswith("https://"):
                    if _cleaned not in CORS_ALLOWED_ORIGINS:
                        CORS_ALLOWED_ORIGINS.append(_cleaned)
                    if _cleaned not in CSRF_TRUSTED_ORIGINS:
                        CSRF_TRUSTED_ORIGINS.append(_cleaned)
                else:
                    for _scheme in ("https://", "http://"):
                        _with_scheme = f"{_scheme}{_cleaned}"
                        if _with_scheme not in CORS_ALLOWED_ORIGINS:
                            CORS_ALLOWED_ORIGINS.append(_with_scheme)
                        if _with_scheme not in CSRF_TRUSTED_ORIGINS:
                            CSRF_TRUSTED_ORIGINS.append(_with_scheme)

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

# Conditionally add GIS if GDAL/PostGIS is available
if os.getenv("ENABLE_GIS", "False").lower() in ("true", "1"):
    INSTALLED_APPS.append('django.contrib.gis')

INSTALLED_APPS += [
    # Third party
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'drf_spectacular',
    'corsheaders',
    # Local apps
    'apps.accounts',
    'apps.government',
    'apps.projects',
    'apps.applications',
    'apps.permits',
    'apps.inspections',
    'apps.monitoring',
    'apps.documents',
    'apps.digital_eye',
    'apps.evidence',
    'apps.stakeholders',
    'apps.settings',
    'apps.analytics',
    'apps.bim',
    'apps.compliance',
    'apps.approvals',
    'apps.notifications',
    'apps.audit',
    'apps.emergency',
    'apps.public_portal',
    'apps.common',
    'apps.processing',
    'apps.reports',
    'apps.scans',
    'apps.storage',
]

import cloudinary
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME", ""),
    api_key=os.getenv("CLOUDINARY_API_KEY", ""),
    api_secret=os.getenv("CLOUDINARY_API_SECRET", "")
)

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'common.middleware.audit.AuditMiddleware',
]

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

# Resend Email Configuration
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "Nexucon Email notifications <notifications@nexucon.net>")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://nexucon-frontend-8x3a.vercel.app")

_default_postgres_engine = 'django.contrib.gis.db.backends.postgis' if os.getenv("ENABLE_GIS", "False").lower() in ("true", "1") else 'django.db.backends.postgresql'

DATABASES = {
    'default': {
        'ENGINE': os.getenv('DATABASE_ENGINE', _default_postgres_engine if (os.getenv('DATABASE_URL') or os.getenv('POSTGRES_DB')) else 'django.db.backends.sqlite3'),
        'NAME': os.getenv('DATABASE_NAME', str(BASE_DIR / 'db.sqlite3')),
        'USER': os.getenv('DATABASE_USER', 'postgres'),
        'PASSWORD': os.getenv('DATABASE_PASSWORD', ''),
        'HOST': os.getenv('DATABASE_HOST', 'localhost'),
        'PORT': os.getenv('DATABASE_PORT', '5432'),
    }
}

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'apps.accounts.authentication.CookieJWTAuthentication',
        'apps.accounts.authentication.ApiKeyAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    # Without a filter backend, every `filterset_fields = [...]` declaration
    # in the viewsets is SILENTLY IGNORED — ?project=... etc. returned every
    # row the user could see, leaking elements across projects (e.g.
    # /digital-eye/bim-elements/?project=X returning other projects' rows).
    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
    ),
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'EXCEPTION_HANDLER': 'common.exceptions.handler.custom_exception_handler',
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=2),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': False,
    'AUTH_COOKIE': 'access_token',
    'AUTH_COOKIE_REFRESH': 'refresh_token',
    'AUTH_COOKIE_SECURE': not DEBUG, # True in production
    'AUTH_COOKIE_HTTP_ONLY': True,
    'AUTH_COOKIE_PATH': '/',
    'AUTH_COOKIE_SAMESITE': 'None',
}

SPECTACULAR_SETTINGS = {
    'TITLE': 'Nexucon Government Agency API',
    'DESCRIPTION': 'Enterprise Building Collapse Prevention & Digital Regulatory Agency API',
    'VERSION': '1.0.0',
    'SERVERS': [
        {'url': 'https://api.nexucon.net', 'description': 'Live Production (VPS)'},
        {'url': 'https://nexucon-backend.onrender.com', 'description': 'Live Production (Render)'},
        {'url': 'http://127.0.0.1:8000', 'description': 'Local Development'},
    ],
}

AUTH_USER_MODEL = 'accounts.User'

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True
STATIC_URL = 'static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Cloudflare R2 / S3 Document Storage
STORAGE_PROVIDER = os.getenv('STORAGE_PROVIDER', '').lower()  # 'cloudflare_r2' enables R2 as default Django storage
CLOUDFLARE_ACCOUNT_ID = os.getenv('CLOUDFLARE_ACCOUNT_ID') or os.getenv('CLOUDFLARE_R2_ACCOUNT_ID', '')
CLOUDFLARE_R2_BUCKET_NAME = os.getenv('CLOUDFLARE_R2_BUCKET_NAME', 'nexucondocument')
CLOUDFLARE_R2_ENDPOINT_URL = os.getenv('CLOUDFLARE_R2_ENDPOINT_URL', f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com" if CLOUDFLARE_ACCOUNT_ID else '')
CLOUDFLARE_R2_ACCESS_KEY_ID = os.getenv('CLOUDFLARE_R2_ACCESS_KEY_ID')
CLOUDFLARE_R2_SECRET_ACCESS_KEY = os.getenv('CLOUDFLARE_R2_SECRET_ACCESS_KEY')

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

if STORAGE_PROVIDER == 'cloudflare_r2' and CLOUDFLARE_R2_ACCESS_KEY_ID and CLOUDFLARE_R2_SECRET_ACCESS_KEY:
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
        "OPTIONS": {
            "access_key": CLOUDFLARE_R2_ACCESS_KEY_ID,
            "secret_key": CLOUDFLARE_R2_SECRET_ACCESS_KEY,
            "bucket_name": CLOUDFLARE_R2_BUCKET_NAME,
            "endpoint_url": CLOUDFLARE_R2_ENDPOINT_URL,
            "signature_version": "s3v4",
            "region_name": "auto",
            "file_overwrite": False,
            "default_acl": None,
            "querystring_auth": True,
            "querystring_expire": 3600,
        },
    }
MEDIA_URL = '/media/'

# Google Maps Static API key for the NDT report's site location map (C6):
# a 500 m radius map image around the project's recorded GNSS coordinates.
# Empty/absent means the report honestly falls back to the operator's map
# photo / BIM captures — no map is ever fabricated.
GOOGLE_MAPS_API_KEY = os.getenv('GOOGLE_MAPS_API_KEY', '')
MEDIA_ROOT = BASE_DIR / 'media'

# Google Cloud Service Account & Translation / Calendar APIs
_google_sa_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', str(BASE_DIR / 'config' / 'google_service_account.json'))
if not os.path.isabs(_google_sa_path):
    _google_sa_path = str(BASE_DIR / _google_sa_path)
GOOGLE_SERVICE_ACCOUNT_FILE = _google_sa_path
GOOGLE_CLOUD_PROJECT_ID = os.getenv('GOOGLE_CLOUD_PROJECT_ID', 'serious-water-469715-f9')

# Google Meet & Calendar service account (nexucon-meeting@serious-water-469715-f9)
GOOGLE_MEETING_PROJECT_ID = os.getenv('GOOGLE_MEETING_PROJECT_ID', GOOGLE_CLOUD_PROJECT_ID)
GOOGLE_MEETING_CLIENT_EMAIL = os.getenv('GOOGLE_MEETING_CLIENT_EMAIL', '')
GOOGLE_MEETING_PRIVATE_KEY = os.getenv('GOOGLE_MEETING_PRIVATE_KEY', '').replace('\\n', '\n')

# ======================================================================
# Trimble Connect Platform API (OAuth 2.0 + PKCE) — names only, never
# committed values. Obtain credentials from the Trimble Developer Console.
# ======================================================================
TRIMBLE_CLIENT_ID = os.getenv('TRIMBLE_CLIENT_ID', '')
TRIMBLE_CLIENT_SECRET = os.getenv('TRIMBLE_CLIENT_SECRET', '')
TRIMBLE_REDIRECT_URI = os.getenv('TRIMBLE_REDIRECT_URI', '')
TRIMBLE_AUTHORIZE_URL = os.getenv(
    'TRIMBLE_AUTHORIZE_URL',
    'https://app.connect.trimble.com/connect/oauth/authorize',
)
TRIMBLE_TOKEN_URL = os.getenv(
    'TRIMBLE_TOKEN_URL',
    'https://app.connect.trimble.com/connect/oauth/token',
)
TRIMBLE_API_BASE = os.getenv(
    'TRIMBLE_API_BASE',
    'https://app.connect.trimble.com/connect/api',
)



# ======================================================================
# Celery broker & task routing (names only — no credentials committed).
# ======================================================================
CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', '')
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', '')

CELERY_TASK_ALWAYS_EAGER = os.getenv('CELERY_TASK_ALWAYS_EAGER', 'False').lower() in ('true', '1')
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE

# ======================================================================
# Django Channels layer for WebSocket processing-status streams. Uses Redis
# when a broker URL is configured; the in-memory layer otherwise (single
# process — development / tests only).
# ======================================================================
if CELERY_BROKER_URL:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {'hosts': [CELERY_BROKER_URL]},
        },
    }
else:
    CHANNEL_LAYERS = {
        'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'},
    }
