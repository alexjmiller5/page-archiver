"""Explicit, independently authorized download of a retained capture."""

import json
from pathlib import Path

from .client import HubError
from .publication import ARTIFACTS


async def retrieve(hub, settings, attempt_id: str, output: Path) -> dict:
    columns = [
        "id",
        "status",
        "source_url",
        "captured_at",
        "failure_code",
        "failure_detail",
        *[f"{kind}_{field}" for kind in ARTIFACTS for field in ("key", "mime", "bytes", "sha256")],
    ]
    result = await hub.rows(settings.capture_table, columns, where={"id": attempt_id})
    if (
        len(result["rows"]) != 1
        or result["rows"][0].get("id") != attempt_id
        or result["rows"][0].get("status") not in {"succeeded", "partial"}
    ):
        raise HubError("capture_unavailable", fatal=True)
    row = result["rows"][0]
    try:
        output.mkdir(mode=0o700)
    except FileExistsError:
        raise HubError("destination_exists", fatal=True) from None
    try:
        for kind in ARTIFACTS:
            await hub.download(
                row[f"{kind}_key"],
                output / f"page.{kind}",
                {k: row.get(f"{kind}_{k}") for k in ("mime", "bytes", "sha256")},
            )
        (output / "metadata.json").write_text(json.dumps(row, indent=2))
        (output / "metadata.json").chmod(0o600)
    except BaseException:
        for name in ("page.html", "page.png", "metadata.json"):
            (output / name).unlink(missing_ok=True)
        output.rmdir()
        raise
    return {
        "id": attempt_id,
        "output": str(output),
        "status": row["status"],
        "warning": row.get("failure_detail"),
    }
