from __future__ import annotations
import base64, os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get('FLEET_MANAGER_DATA_DIR', ROOT / 'data'))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.environ.get('DATABASE_URL', f"sqlite:///{DATA_DIR / 'fleet_manager.db'}")
MASTER_ENCRYPTION_KEY = os.environ.get('MASTER_ENCRYPTION_KEY')
SESSION_SECRET = os.environ.get('SESSION_SECRET')
APP_TIMEZONE = os.environ.get('APP_TIMEZONE', 'America/Chicago')
ADMIN_EMAIL = os.environ.get('FLEET_ADMIN_EMAIL', 'admin@example.local')
ADMIN_PASSWORD = os.environ.get('FLEET_ADMIN_PASSWORD')
SESSION_COOKIE = 'hafm_session'
CSRF_HEADER = 'x-csrf-token'

def require_secret(name: str, value: str | None) -> str:
    if not value:
        raise RuntimeError(f'{name} is required')
    return value

def get_master_key() -> bytes:
    raw = require_secret('MASTER_ENCRYPTION_KEY', MASTER_ENCRYPTION_KEY)
    try:
        b = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))
    except Exception as exc:
        raise RuntimeError('MASTER_ENCRYPTION_KEY must be urlsafe base64 encoded 32 bytes') from exc
    if len(b) != 32:
        raise RuntimeError('MASTER_ENCRYPTION_KEY must decode to 32 bytes')
    return b
