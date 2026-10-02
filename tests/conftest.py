import base64
import os
import tempfile
from pathlib import Path

# Set one isolated database before any application module is imported during test
# collection. This prevents focused/new test files from ever opening the live DB.
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="hafm-pytest-"))
os.environ["MASTER_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(b"0" * 32).decode()
os.environ["FLEET_ADMIN_PASSWORD"] = "test-password"
os.environ["DATABASE_URL"] = "sqlite:///" + str(_TEST_ROOT / "test.db")
