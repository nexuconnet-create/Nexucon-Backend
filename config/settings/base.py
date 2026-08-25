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

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dummy-secret-key-for-dev")
DEBUG = os.getenv("DJANGO_DEBUG", "True") == "True"
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",")
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_ALL_HEADERS = True
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://192.168.0.196:3000",
]
CSRF_TRUSTED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "https://*.vercel.app",
    "https://nexucon-backend.onrender.com",
]

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
]

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

DATABASES = {
    'default': {
        'ENGINE': os.getenv('DATABASE_ENGINE', 'django.contrib.gis.db.backends.postgis' if (os.getenv('DATABASE_URL') or os.getenv('POSTGRES_DB')) else 'django.db.backends.sqlite3'),
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
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
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
CLOUDFLARE_ACCOUNT_ID = os.getenv('CLOUDFLARE_ACCOUNT_ID', 'ba64cd9c51c2da4db93a1886397fd7b3')
CLOUDFLARE_R2_BUCKET_NAME = os.getenv('CLOUDFLARE_R2_BUCKET_NAME', 'nexucondocument')
CLOUDFLARE_R2_ACCESS_KEY_ID = os.getenv('CLOUDFLARE_R2_ACCESS_KEY_ID')
CLOUDFLARE_R2_SECRET_ACCESS_KEY = os.getenv('CLOUDFLARE_R2_SECRET_ACCESS_KEY')

if CLOUDFLARE_R2_ACCESS_KEY_ID and CLOUDFLARE_R2_SECRET_ACCESS_KEY:
    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
            "OPTIONS": {
                "access_key": CLOUDFLARE_R2_ACCESS_KEY_ID,
                "secret_key": CLOUDFLARE_R2_SECRET_ACCESS_KEY,
                "bucket_name": CLOUDFLARE_R2_BUCKET_NAME,
                "endpoint_url": f"https://{CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com",
                "signature_version": "s3v4",
                "region_name": "auto",
                "file_overwrite": False,
                "default_acl": None,
                "querystring_auth": True,
                "querystring_expire": 3600,
            },
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
else:
    MEDIA_URL = '/media/'
    MEDIA_ROOT = BASE_DIR / 'media'

# Google Cloud Service Account & Translation / Calendar APIs
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', str(BASE_DIR / 'config' / 'google_service_account.json'))
GOOGLE_CLOUD_PROJECT_ID = os.getenv('GOOGLE_CLOUD_PROJECT_ID', 'serious-water-469715-f9')

