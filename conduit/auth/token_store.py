import json
import os
import stat
from pathlib import Path
from typing import Optional


class TokenStore:
    """Persists OAuth2 tokens to disk with restrictive file permissions."""

    DEFAULT_PATH = Path.home() / ".conduit" / "oauth_tokens.json"

    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else self.DEFAULT_PATH

    def load(self, key: str) -> Optional[dict]:
        """Return the stored token dict for *key*, or None if absent/unreadable."""
        try:
            with open(self._path, "r") as f:
                data = json.load(f)
            return data.get(key)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def save(self, key: str, token: dict) -> None:
        """Persist *token* under *key*, creating the file with 0600 permissions."""
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Load existing data so we don't overwrite other keys.
        existing: dict = {}
        try:
            with open(self._path, "r") as f:
                existing = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

        existing[key] = token

        # Write to a temp file first, then atomically replace.
        tmp_path = self._path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w") as f:
                json.dump(existing, f, indent=2)
            # Set 0600 before the rename so the token is never world-readable.
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
            tmp_path.replace(self._path)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise

    def delete(self, key: str) -> bool:
        """Remove the token for *key*.  Returns True if a token was removed."""
        existing = {}
        try:
            with open(self._path, "r") as f:
                existing = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False

        if key not in existing:
            return False

        del existing[key]
        tmp_path = self._path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w") as f:
                json.dump(existing, f, indent=2)
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
            tmp_path.replace(self._path)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
        return True
