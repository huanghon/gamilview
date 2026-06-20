from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailClientError(RuntimeError):
    pass


class GmailAccountClient:
    def __init__(self, account: str, credentials_dir: str | Path):
        self.account = account
        self.credentials_dir = Path(credentials_dir)
        self.credentials_path = self._find_credentials_path()
        self.token_path = self._find_token_path()
        self.service = self._build_service()

    def _candidate_credentials_paths(self) -> list[Path]:
        base = self.credentials_dir
        return [
            base / self.account / "credentials.json",
            base / f"{self.account}_credentials.json",
            base / f"credentials_{self.account}.json",
            base / f"client_secret_{self.account}.json",
            base / "credentials.json",
        ]

    def _candidate_token_paths(self) -> list[Path]:
        base = self.credentials_dir
        return [
            base / self.account / "token.json",
            base / f"{self.account}_token.json",
            base / f"token_{self.account}.json",
        ]

    def _find_credentials_path(self) -> Path:
        for path in self._candidate_credentials_paths():
            if path.exists():
                return path
        checked = ", ".join(str(p) for p in self._candidate_credentials_paths())
        raise GmailClientError(
            f"Credentials file not found for {self.account}. Checked: {checked}"
        )

    def _find_token_path(self) -> Path:
        candidates = self._candidate_token_paths()
        for path in candidates:
            if path.exists():
                return path
        return candidates[0]

    def _build_service(self) -> Any:
        creds = None
        if self.token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
            except json.JSONDecodeError as exc:
                raise GmailClientError(
                    f"Token file is empty or corrupted (invalid JSON) at {self.token_path}: {exc}"
                ) from exc
            except Exception as exc:
                raise GmailClientError(f"Failed to read token file {self.token_path}: {exc}") from exc

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self.token_path.parent.mkdir(parents=True, exist_ok=True)
                self.token_path.write_text(creds.to_json(), encoding="utf-8")
            except Exception as exc:
                raise GmailClientError(f"Failed to refresh token for {self.account}: {exc}") from exc

        if not creds or not creds.valid:
            raise GmailClientError(
                f"Token is missing or invalid for {self.account}. Run: "
                f"python -m gmail.client authorize {self.account} --dir {self.credentials_dir}"
            )

        try:
            return build("gmail", "v1", credentials=creds, cache_discovery=False)
        except json.JSONDecodeError as exc:
            raise GmailClientError(
                f"Google API Discovery returned an invalid JSON response. This usually happens "
                f"when the network is blocked (e.g. inside China or behind a proxy/firewall returning HTML): {exc}"
            ) from exc
        except Exception as exc:
            raise GmailClientError(f"Failed to build Gmail service for {self.account}: {exc}") from exc

    def search_messages(self, query: str, max_results: int = 20) -> list[dict[str, Any]]:
        try:
            response = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, maxResults=max_results)
                .execute()
            )
            return response.get("messages", [])
        except HttpError as exc:
            raise GmailClientError(f"Gmail search failed for {self.account}: {exc}") from exc

    def get_message(self, message_id: str) -> dict[str, Any]:
        try:
            return (
                self.service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except HttpError as exc:
            raise GmailClientError(
                f"Gmail message fetch failed for {self.account}/{message_id}: {exc}"
            ) from exc


def authorize_account(account: str, credentials_dir: str | Path) -> Path:
    credentials_dir = Path(credentials_dir)
    temp = object.__new__(GmailAccountClient)
    temp.account = account
    temp.credentials_dir = credentials_dir
    credentials_path = GmailAccountClient._find_credentials_path(temp)
    token_path = GmailAccountClient._find_token_path(temp)

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return token_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail OAuth helper")
    subparsers = parser.add_subparsers(dest="command", required=True)
    authorize = subparsers.add_parser("authorize", help="Create token.json for one account")
    authorize.add_argument("account", help="Account alias from config/phones.json, for example gmail1")
    authorize.add_argument("--dir", default="gmail_credentials", help="Credentials directory")
    args = parser.parse_args()

    if args.command == "authorize":
        token_path = authorize_account(args.account, args.dir)
        print(f"Token saved to {token_path}")


if __name__ == "__main__":
    main()
