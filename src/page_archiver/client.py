"""Bounded hub API transport. Credentials never leave the configured origin."""

import asyncio
import json
import re
import subprocess
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from page_archiver.config import Settings
from page_archiver.state import Batch


class HubError(Exception):
    def __init__(self, code: str, retry_after: float | None = None, *, fatal: bool = False):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.fatal = fatal


def retry_delay(headers, now: datetime | None = None) -> float:
    value = httpx.Headers(headers).get("Retry-After", "")
    if re.fullmatch(r"[0-9]+", value):
        digits = value.lstrip("0") or "0"
        seconds = 3600 if len(digits) > 4 else int(digits)
    else:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                raise ValueError("timezone missing")
            seconds = (deadline - (now or datetime.now(UTC))).total_seconds()
        except (ValueError, TypeError, OverflowError):
            seconds = 60
    return min(3600, max(1, seconds))


def credential(settings: Settings) -> str:
    if settings.hub_token is not None:
        token = settings.hub_token.get_secret_value()
    elif settings.credential_command:
        try:
            result = subprocess.run(
                settings.credential_command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
                check=True,
            )
            token = result.stdout.decode().strip()
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise HubError("credential_command_failed", fatal=True) from None
    else:
        raise HubError("credential_required", fatal=True)
    if not token or len(token) > 4096 or any(c.isspace() or ord(c) < 32 for c in token):
        raise HubError("invalid_credential", fatal=True)
    return token


def identifier(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None


class HubClient:
    def __init__(self, settings: Settings, *, transport=None):
        if not all(
            (
                settings.hub_url,
                settings.subscription_id,
                settings.capture_table,
                settings.artifact_prefix,
            )
        ):
            raise HubError("hub_configuration_required", fatal=True)
        self.settings = settings
        self.transport = transport
        self.client: httpx.AsyncClient | None = None
        self.scopes: set[str] | None = None

    async def __aenter__(self):
        token = credential(self.settings)
        self.client = httpx.AsyncClient(
            base_url=self.settings.hub_url,
            transport=self.transport,
            headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"},
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(40, connect=10),
        )
        return self

    async def __aexit__(self, *_):
        if self.client is not None:
            await self.client.aclose()

    async def request(self, method, path, *, maximum=1_048_576, permitted=(200,), **kwargs):
        assert self.client is not None
        try:
            async with (
                asyncio.timeout(180 if method == "PUT" else 40),
                self.client.stream(method, path, **kwargs) as response,
            ):
                status = response.status_code
                if status not in permitted:
                    if 300 <= status < 400:
                        raise HubError("redirect_refused", fatal=True)
                    if status in (401, 403):
                        raise HubError("credential_rejected", fatal=True)
                    if status == 429:
                        raise HubError("hub_capped", retry_delay(response.headers))
                    if status >= 500:
                        raise HubError("hub_unavailable")
                    raise HubError("hub_request_rejected", fatal=True)
                if method == "HEAD":
                    return status, response.headers, None
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > maximum:
                        raise HubError("response_too_large", fatal=True)
                    data.extend(chunk)
                try:
                    body = json.loads(data)
                except (ValueError, UnicodeError):
                    raise HubError("invalid_hub_response", fatal=True) from None
                if not isinstance(body, dict):
                    raise HubError("invalid_hub_response", fatal=True)
                return status, response.headers, body
        except (httpx.HTTPError, OSError, TimeoutError):
            raise HubError("hub_unavailable") from None

    async def session(self) -> dict:
        _, _, body = await self.request("GET", "/v1/session", maximum=65_536)
        caps, scopes = body.get("capabilities"), body.get("scopes")
        if (
            not isinstance(caps, dict)
            or caps.get("row_api") != "v1"
            or caps.get("subscriptions") != "durable-pull-v1"
            or caps.get("files") != "opaque-key-v1"
        ):
            raise HubError("unsupported_hub_protocol", fatal=True)
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise HubError("invalid_hub_response", fatal=True)
        self.scopes = set(scopes)
        if self.scopes.intersection({"admin", "full", "tables:read", "tables:write"}):
            raise HubError("credential_too_broad", fatal=True)
        required = {
            f"subscriptions:consume:{self.settings.subscription_id}",
            f"tables:read:{self.settings.capture_table}",
            f"tables:write:{self.settings.capture_table}",
            f"files:read:{self.settings.artifact_prefix}",
            f"files:write:{self.settings.artifact_prefix}",
        }
        if not required.issubset(self.scopes):
            raise HubError("credential_scope_missing", fatal=True)
        return body

    @property
    def subscription_path(self) -> str:
        return f"/v1/subscriptions/{self.settings.subscription_id}"

    async def subscription(self) -> dict:
        _, _, body = await self.request("GET", self.subscription_path)
        if (
            body.get("id") != self.settings.subscription_id
            or body.get("protocol") != "durable-pull-v1"
            or body.get("state") not in {"active", "paused", "retired"}
            or not isinstance(body.get("sources"), list)
            or not body["sources"]
        ):
            raise HubError("invalid_subscription", fatal=True)
        if self.scopes is None:
            await self.session()
        for source in body["sources"]:
            if (
                not isinstance(source, dict)
                or not identifier(source.get("table"))
                or not isinstance(source.get("columns"), list)
                or not source["columns"]
                or not all(identifier(c) for c in source["columns"])
            ):
                raise HubError("invalid_subscription", fatal=True)
            if f"tables:read:{source['table']}" not in self.scopes:
                raise HubError("source_not_authorized", fatal=True)
        return body

    async def poll(self, wait: int = 30) -> dict:
        _, _, body = await self.request("GET", self.subscription_path + f"/events?wait={wait}")
        try:
            batch = Batch.model_validate(body)
            if batch.subscription_id != self.settings.subscription_id:
                raise ValueError("wrong subscription")
        except (ValueError, ValidationError):
            raise HubError("invalid_delivery", fatal=True) from None
        return batch.model_dump()

    async def ack(self, delivery_id: str) -> str:
        status, _, body = await self.request(
            "POST",
            self.subscription_path + "/ack",
            json={"delivery_id": delivery_id},
            permitted=(200, 409),
            maximum=4096,
        )
        if status == 409:
            raise HubError("delivery_conflict", fatal=True)
        seq = body.get("acked_seq")
        if not isinstance(seq, str) or re.fullmatch(r"0|[1-9][0-9]*", seq) is None:
            raise HubError("invalid_acknowledgment", fatal=True)
        return seq

    async def rows(self, table: str, columns: list[str], *, where=None, after=None) -> dict:
        if not identifier(table) or not columns or not all(identifier(c) for c in columns):
            raise HubError("invalid_row_request", fatal=True)
        request = {"table": table, "columns": columns, "limit": 100}
        if where is not None:
            request["where"] = where
        if after is not None:
            request["after"] = after
        _, _, body = await self.request("POST", "/v1/rows/pull", json=request)
        if (
            not isinstance(body.get("rows"), list)
            or len(body["rows"]) > 100
            or not all(isinstance(row, dict) for row in body["rows"])
            or (body.get("next_cursor") is not None and not isinstance(body["next_cursor"], str))
        ):
            raise HubError("invalid_rows", fatal=True)
        return body

    async def insert(self, table: str, row: dict) -> dict:
        if not identifier(table) or not all(identifier(c) for c in row):
            raise HubError("invalid_row_request", fatal=True)
        _, _, body = await self.request(
            "POST", "/v1/rows/insert", json={"table": table, "columns": list(row), "rows": [row]}
        )
        if not all(isinstance(body.get(k), list) for k in ("inserted", "existing", "rejected")):
            raise HubError("invalid_write_response", fatal=True)
        return body

    def file_path(self, key: str) -> str:
        if (
            not isinstance(key, str)
            or not key.startswith(self.settings.artifact_prefix)
            or any(c in key for c in ("%", "\\"))
            or any(ord(c) < 32 or ord(c) == 127 for c in key)
            or any(part in {"", ".", ".."} for part in key.split("/"))
        ):
            raise HubError("invalid_file_key", fatal=True)
        return "/v1/files/" + quote(key, safe="/")

    async def upload(self, key: str, path: Path, mime: str, sha256: str) -> dict | None:
        async def chunks():
            with path.open("rb") as source:
                while chunk := source.read(65536):
                    yield chunk

        status, _, body = await self.request(
            "PUT",
            self.file_path(key),
            content=chunks(),
            maximum=16_384,
            permitted=(201, 412),
            headers={
                "If-None-Match": "*",
                "X-Content-SHA256": sha256,
                "Content-Type": mime,
                "Content-Length": str(path.stat().st_size),
            },
        )
        if status == 412:
            return None
        if (
            body.get("key") != key
            or body.get("mime") != mime
            or body.get("bytes") != path.stat().st_size
            or body.get("sha256") != sha256
        ):
            raise HubError("unverified_artifact", fatal=True)
        return body

    async def head(self, key: str) -> dict:
        _, headers, _ = await self.request("HEAD", self.file_path(key))
        checksum, length, mime = (
            headers.get(k) for k in ("X-Content-SHA256", "Content-Length", "Content-Type")
        )
        if (
            not checksum
            or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
            or not length
            or not length.isdigit()
            or not mime
        ):
            raise HubError("unverified_artifact", fatal=True)
        return {"mime": mime, "bytes": int(length), "sha256": checksum, "etag": headers.get("ETag")}
