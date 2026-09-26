from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict


BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "development-only-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() in {"1", "true", "yes"}
ALLOWED_HOSTS = [host.strip() for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if host.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, "").split(",") if value.strip())

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.identity_access",
    "apps.governance",
    "apps.document_intake",
    "apps.search_ui",
    "apps.documents",
    "apps.knowledge_graph_ui",
    "apps.system_ui",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.governance.middleware.CorrelationIdMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.identity_access.middleware.AccessControlMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.identity_access.context.access_context",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
if _env_bool("DJANGO_TEST_SQLITE"):
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
else:
    _database = conninfo_to_dict(os.environ["DATABASE_URL"])
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _database.get("dbname"),
            "USER": _database.get("user"),
            "PASSWORD": _database.get("password"),
            "HOST": _database.get("host", "127.0.0.1"),
            "PORT": _database.get("port", "5434"),
            "CONN_MAX_AGE": int(os.getenv("DJANGO_DB_CONN_MAX_AGE", "60")),
        }
    }
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "identity_access:login"
LOGIN_REDIRECT_URL = "search_ui:search"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = _env_bool("DJANGO_SECURE_COOKIES")
SESSION_COOKIE_AGE = int(os.getenv("DJANGO_SESSION_AGE_SECONDS", "28800"))
SESSION_EXPIRE_AT_BROWSER_CLOSE = _env_bool("DJANGO_SESSION_EXPIRE_AT_BROWSER_CLOSE", True)
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = _env_bool("DJANGO_SECURE_COOKIES")
SECURE_SSL_REDIRECT = _env_bool("DJANGO_SECURE_SSL_REDIRECT")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

ACCESS_ALLOWED_EMAIL_DOMAINS = tuple(value.casefold() for value in _env_list("ACCESS_ALLOWED_EMAIL_DOMAINS"))
ACCESS_ALLOWED_ORGANIZATION_CODES = tuple(value.casefold() for value in _env_list("ACCESS_ALLOWED_ORGANIZATION_CODES"))
ACCESS_ALIAS_NORMALIZATION_DOMAINS = tuple(value.casefold() for value in _env_list("ACCESS_ALIAS_NORMALIZATION_DOMAINS"))
ACCESS_EMAIL_ALIAS_SEPARATOR = os.getenv("ACCESS_EMAIL_ALIAS_SEPARATOR", "+")[:1]
ACCESS_TRUSTED_PROXY_NETWORKS = _env_list("ACCESS_TRUSTED_PROXY_NETWORKS")
ACCESS_REQUEST_EXPIRY_DAYS = int(os.getenv("ACCESS_REQUEST_EXPIRY_DAYS", "14"))
ACCESS_REJECTION_COOLDOWN_HOURS = int(os.getenv("ACCESS_REJECTION_COOLDOWN_HOURS", "72"))
ACCESS_ACTIVATION_TOKEN_HOURS = int(os.getenv("ACCESS_ACTIVATION_TOKEN_HOURS", "24"))
ACCESS_REQUEST_SOURCE_LIMIT = int(os.getenv("ACCESS_REQUEST_SOURCE_LIMIT", "10"))
ACCESS_REQUEST_EMAIL_LIMIT = int(os.getenv("ACCESS_REQUEST_EMAIL_LIMIT", "4"))
ACCESS_REQUEST_IDENTITY_LIMIT = int(os.getenv("ACCESS_REQUEST_IDENTITY_LIMIT", "4"))
ACCESS_REQUEST_WINDOW_SECONDS = int(os.getenv("ACCESS_REQUEST_WINDOW_SECONDS", "3600"))
ACCESS_LOGIN_SOURCE_LIMIT = int(os.getenv("ACCESS_LOGIN_SOURCE_LIMIT", "12"))
ACCESS_LOGIN_ACCOUNT_SOURCE_LIMIT = int(os.getenv("ACCESS_LOGIN_ACCOUNT_SOURCE_LIMIT", "6"))
ACCESS_LOGIN_WINDOW_SECONDS = int(os.getenv("ACCESS_LOGIN_WINDOW_SECONDS", "900"))
ACCESS_THROTTLE_BLOCK_SECONDS = int(os.getenv("ACCESS_THROTTLE_BLOCK_SECONDS", "900"))

EMAIL_BACKEND = os.getenv("DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")

# Stage 3 secure document intake. Paths are generated internally; user supplied
# names are metadata only and never participate in filesystem resolution.
DOCUMENT_QUARANTINE_ROOT = Path(
    os.getenv("DOCUMENT_QUARANTINE_ROOT", str(BASE_DIR.parent / "Government_Documents" / "Incoming" / "Officer_Uploads"))
).resolve()
DOCUMENT_APPROVED_ROOT = Path(
    os.getenv(
        "APPROVED_SUBMISSION_ROOT",
        os.getenv("DOCUMENT_APPROVED_ROOT", str(BASE_DIR.parent / "Government_Documents" / "Approved_Submissions")),
    )
).resolve()
DOCUMENT_MAX_UPLOAD_BYTES = int(os.getenv("DOCUMENT_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
DOCUMENT_MAX_PAGES = int(os.getenv("DOCUMENT_MAX_PAGES", "500"))
DOCUMENT_PREVIEW_PAGES = int(os.getenv("DOCUMENT_PREVIEW_PAGES", "3"))
DOCUMENT_PREVIEW_CHARACTERS = int(os.getenv("DOCUMENT_PREVIEW_CHARACTERS", "6000"))
DOCUMENT_VALIDATION_TIMEOUT_SECONDS = int(os.getenv("DOCUMENT_VALIDATION_TIMEOUT_SECONDS", "30"))
DOCUMENT_INGESTION_MAX_ATTEMPTS = int(os.getenv("DOCUMENT_INGESTION_MAX_ATTEMPTS", "3"))
DOCUMENT_INGESTION_LEASE_SECONDS = int(os.getenv("DOCUMENT_INGESTION_LEASE_SECONDS", "900"))
DOCUMENT_INGESTION_HEARTBEAT_SECONDS = int(os.getenv("DOCUMENT_INGESTION_HEARTBEAT_SECONDS", "30"))
DOCUMENT_IAS_SELF_CONFIRM_ALLOWED = _env_bool("DOCUMENT_IAS_SELF_CONFIRM_ALLOWED", True)
_document_scanner_setting = os.getenv("DOCUMENT_MALWARE_SCANNER", "AUTO").strip().upper()
_windows_defender_cli = Path(r"C:\Program Files\Windows Defender\MpCmdRun.exe")
if _document_scanner_setting == "AUTO" and _windows_defender_cli.is_file():
    DOCUMENT_MALWARE_SCANNER = "WINDOWS_DEFENDER"
    DOCUMENT_MALWARE_SCANNER_EXECUTABLE = str(_windows_defender_cli)
elif _document_scanner_setting in {"", "NONE", "AUTO"}:
    DOCUMENT_MALWARE_SCANNER = ""
    DOCUMENT_MALWARE_SCANNER_EXECUTABLE = ""
else:
    DOCUMENT_MALWARE_SCANNER = _document_scanner_setting
    DOCUMENT_MALWARE_SCANNER_EXECUTABLE = os.getenv("DOCUMENT_MALWARE_SCANNER_EXECUTABLE", "").strip()
