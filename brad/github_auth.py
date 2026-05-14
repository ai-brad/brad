"""GitHub authentication helpers for PAT and GitHub App installs."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import jwt
import requests

from brad.logging_config import get_logger


class GitHubTokenProvider:
    """Return a usable GitHub token for REST API and git-over-HTTPS access."""

    _REFRESH_SKEW_SECONDS = 300

    def __init__(
        self,
        *,
        github_token: str = "",
        github_app_id: Optional[str] = None,
        github_app_installation_id: Optional[str] = None,
        github_app_private_key: Optional[str] = None,
        github_app_private_key_path: Optional[str] = None,
    ):
        self.logger = get_logger(__name__)
        self.github_token = github_token or ""
        self.github_app_id = github_app_id or None
        self.github_app_installation_id = github_app_installation_id or None
        self.github_app_private_key = self._load_private_key(
            github_app_private_key=github_app_private_key,
            github_app_private_key_path=github_app_private_key_path,
        )
        self._cached_installation_token = ""
        self._cached_installation_token_expires_at = 0.0

    @classmethod
    def from_config(cls, cfg) -> "GitHubTokenProvider":
        return cls(
            github_token=getattr(cfg, "github_token", "") or "",
            github_app_id=getattr(cfg, "github_app_id", None),
            github_app_installation_id=getattr(cfg, "github_app_installation_id", None),
            github_app_private_key=getattr(cfg, "github_app_private_key", None),
            github_app_private_key_path=getattr(cfg, "github_app_private_key_path", None),
        )

    def is_configured(self) -> bool:
        return bool(self.github_token or self.uses_github_app())

    def uses_github_app(self) -> bool:
        return bool(
            self.github_app_id
            and self.github_app_installation_id
            and self.github_app_private_key
        )

    def get_token(self) -> str:
        if self.uses_github_app():
            return self._get_installation_token()
        if self.github_token:
            return self.github_token
        raise ValueError(
            "GitHub auth is not configured. Set GITHUB_TOKEN or "
            "GITHUB_APP_ID + GITHUB_APP_INSTALLATION_ID + "
            "GITHUB_APP_PRIVATE_KEY[_PATH]."
        )

    def get_headers(self, accept: str = "application/vnd.github+json") -> dict:
        if self.uses_github_app():
            auth_value = f"Bearer {self.get_token()}"
        else:
            auth_value = f"token {self.get_token()}"
        return {
            "Authorization": auth_value,
            "Accept": accept,
        }

    @property
    def auth_scheme(self) -> str:
        return "github_app" if self.uses_github_app() else "pat"

    def redact(self, text: str) -> str:
        if not text:
            return text

        redacted = text
        candidates = [
            self.github_token,
            self._cached_installation_token,
        ]
        for candidate in candidates:
            if candidate:
                redacted = redacted.replace(candidate, "<redacted>")
        return redacted

    def _get_installation_token(self) -> str:
        now = time.time()
        if (
            self._cached_installation_token
            and now < self._cached_installation_token_expires_at - self._REFRESH_SKEW_SECONDS
        ):
            return self._cached_installation_token

        jwt_token = self._build_app_jwt()
        resp = requests.post(
            f"https://api.github.com/app/installations/{self.github_app_installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()

        token = payload.get("token", "")
        expires_at = payload.get("expires_at", "")
        if not token or not expires_at:
            raise RuntimeError("GitHub App token response was missing token or expires_at")

        self._cached_installation_token = token
        self._cached_installation_token_expires_at = self._parse_github_timestamp(expires_at)
        self.logger.info(
            "Minted GitHub App installation token expiring at %s",
            expires_at,
        )
        return token

    def _build_app_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 540,
            "iss": str(self.github_app_id),
        }
        return jwt.encode(payload, self.github_app_private_key, algorithm="RS256")

    @staticmethod
    def _parse_github_timestamp(value: str) -> float:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).timestamp()

    @staticmethod
    def _load_private_key(
        *,
        github_app_private_key: Optional[str],
        github_app_private_key_path: Optional[str],
    ) -> Optional[str]:
        if github_app_private_key:
            return GitHubTokenProvider._normalize_private_key(github_app_private_key)

        if github_app_private_key_path:
            key_path = Path(github_app_private_key_path).expanduser()
            if not key_path.is_file():
                raise ValueError(f"GITHUB_APP_PRIVATE_KEY_PATH does not exist: {key_path}")
            return key_path.read_text(encoding="utf-8")

        return None

    @staticmethod
    def _normalize_private_key(private_key: str) -> str:
        normalized = private_key.strip()
        if "\\n" in normalized:
            normalized = normalized.replace("\\n", "\n")
        if not normalized.endswith("\n"):
            normalized += "\n"
        return normalized


def build_github_token_provider(cfg) -> GitHubTokenProvider:
    return GitHubTokenProvider.from_config(cfg)


def build_github_token_provider_from_env() -> GitHubTokenProvider:
    class _EnvCfg:
        github_token = os.environ.get("GITHUB_TOKEN", "")
        github_app_id = os.environ.get("GITHUB_APP_ID") or None
        github_app_installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID") or None
        github_app_private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY") or None
        github_app_private_key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH") or None

    return build_github_token_provider(_EnvCfg())
