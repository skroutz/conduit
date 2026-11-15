"""
File upload for HTTP deployments, where the server cannot read the caller's
disk and a model cannot emit a real file as a base64 tool argument.

``pha_file_prepare_upload`` hands out a single-use token; the caller then sends
the raw bytes to ``UPLOAD_PATH`` with any HTTP client, and the file is stored
in Phabricator under the credential that prepared the upload.
"""

import asyncio
import base64
import re
import secrets
import shlex
import time
from typing import Callable, Dict, NamedTuple, Optional

from fastmcp import FastMCP
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from conduit.client import PhabricatorAPIError, PhabricatorClient
from conduit.tools.handlers import handle_api_errors

UPLOAD_PATH = "/files/upload"

# The token travels in a header, not in the URL: uvicorn and reverse proxies
# write request paths to their access logs.
UPLOAD_TOKEN_HEADER = "X-Conduit-Upload-Token"  # nosec B105 - header name, not a secret

UPLOAD_TTL_SECONDS = 600

# file.upload is a single form-encoded request, and base64 plus form encoding
# inflate the payload by about 1.42x. The default keeps that request under the
# 32M that PHP's post_max_size is commonly set to on Phabricator hosts.
DEFAULT_MAX_UPLOAD_MB = 16

# Each pending upload pins an authenticated client until it expires.
MAX_PENDING_UPLOADS_PER_USER = 10

# A redeemed token no longer counts as pending, so uploads in flight are
# capped separately. Each holds the body, its base64 form and the encoded
# request in memory (about six times the file) until Phabricator answers.
MAX_ACTIVE_UPLOADS_PER_USER = 2
MAX_ACTIVE_UPLOADS = 8

# A stalled sender must not pin an active slot; 5 minutes still lets a
# maximum-size file through at roughly 56 KB/s.
UPLOAD_BODY_TIMEOUT_SECONDS = 300

# Files are created readable by their author only, unless the deployment
# widens it. A file also becomes readable to whoever can see an object it is
# attached to, but only Phabricator before May 2022 attaches a file embedded
# through Conduit. Later versions and Phorge record the embed as a reference,
# which grants nothing, and have no Conduit call to attach.
DEFAULT_VIEW_POLICY = "no-one"

_MAX_NAME_LENGTH = 255

# Unknown, expired and used tokens share one answer so a caller cannot probe
# which tokens were ever issued.
_REJECTED = {
    "success": False,
    "error": "Unknown, expired or already used upload token",
}


class _PendingUpload(NamedTuple):
    expires_at: float
    user_phid: str
    client: PhabricatorClient
    name: str


def _sanitize_file_name(name: str) -> str:
    """Reduce a caller-supplied name to a bare, storable file name."""
    base = re.split(r"[/\\]", str(name))[-1]
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    if not base.strip("."):
        raise ValueError("name must be a file name, for example 'screenshot.png'")
    return base[:_MAX_NAME_LENGTH]


def _store_file(
    client: PhabricatorClient, name: str, data: bytearray, view_policy: str
) -> dict:
    """Create the file in Phabricator and describe how to reference it."""
    file_phid = client.file.upload_file(
        base64.b64encode(data).decode("ascii"),
        name=name,
        view_policy=view_policy,
    )
    info = client.file.get_file_info(file_phid)
    fields = info.get("fields") or {}
    monogram = f"F{info['id']}"

    return {
        "success": True,
        "file": {
            "id": monogram,
            "phid": file_phid,
            "name": fields.get("name", name),
            "size": len(data),
            "uri": fields.get("uri"),
            "view_policy": view_policy,
        },
        "remarkup": "{" + monogram + "}",
    }


def register_file_upload_tools(
    mcp: FastMCP,
    get_client_func: Callable[[], PhabricatorClient],
    server_url: str,
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_MB * 1024 * 1024,
    view_policy: str = DEFAULT_VIEW_POLICY,
) -> None:
    """
    Register the upload tool and the HTTP endpoint that receives the bytes.

    Args:
        mcp: FastMCP instance to register the tool and the route with
        get_client_func: Function to get the calling user's Phabricator client
        server_url: Public base URL of this server, as clients reach it
        max_upload_bytes: Largest request body the endpoint accepts
        view_policy: Phabricator view policy every uploaded file is created with
    """
    upload_url = server_url.rstrip("/") + UPLOAD_PATH

    # In-process state: a token is only redeemable on the worker that issued
    # it. A multi-worker or multi-replica deployment needs a shared store.
    pending: Dict[str, _PendingUpload] = {}

    # Uploads between token redemption and Phabricator's answer, by user.
    # Only the endpoint, on the event loop, changes it.
    active: Dict[str, int] = {}

    @mcp.tool()
    @handle_api_errors
    def pha_file_prepare_upload(name: str) -> dict:
        """
        Prepare the upload of a local file (image, PDF, log, ...) to Phabricator.

        This server cannot read your disk, so uploading takes two steps:
        1. Call this tool with the file name.
        2. Run the returned `curl_command` in a shell, replacing PATH_TO_FILE
           with the local path. It sends the raw file bytes as the request body.

        The curl response is JSON with the file id (`F123`), its PHID and the
        `remarkup` (`{F123}`) to paste into a task comment or description to
        embed the file. The token works once and expires after 10 minutes;
        prepare again for every file. On a 429 run the same command again.

        The file is owned by you and created with the `view_policy` in the
        response. With `no-one` only you can open it at first: Phabricator
        before May 2022 opens it to a task's readers once it is embedded
        there, while later versions and Phorge need you to choose "Attach"
        for it under "Referenced Files" on the task page. Tell the user when
        that step applies.

        Args:
            name: File name to store, including the extension (e.g. "screenshot.png")

        Returns:
            Upload URL, the header carrying the single-use token, a ready-made
            curl command, the expiry and the size limit
        """
        safe_name = _sanitize_file_name(name)
        client = get_client_func()

        # Header-token mode accepts any 32-character token, so prove the
        # credential is live before the endpoint buffers a body in its name.
        user_phid = client.user.whoami()["phid"]

        # Iterate a snapshot: the endpoint pops entries from the event loop
        # while this tool may run in a worker thread.
        now = time.monotonic()
        held = 0
        for key, entry in list(pending.items()):
            if entry.expires_at <= now:
                pending.pop(key, None)
            elif entry.user_phid == user_phid:
                held += 1

        if held >= MAX_PENDING_UPLOADS_PER_USER:
            raise ValueError(
                "Too many pending uploads; use or let expire the ones already prepared"
            )

        token = secrets.token_urlsafe(32)
        pending[token] = _PendingUpload(
            now + UPLOAD_TTL_SECONDS, user_phid, client, safe_name
        )

        return {
            "success": True,
            "upload_url": upload_url,
            "method": "POST",
            "headers": {UPLOAD_TOKEN_HEADER: token},
            "curl_command": (
                f"curl -sS -H '{UPLOAD_TOKEN_HEADER}: {token}' "
                f"--data-binary @PATH_TO_FILE {shlex.quote(upload_url)}"
            ),
            "name": safe_name,
            "expires_in_seconds": UPLOAD_TTL_SECONDS,
            "max_bytes": max_upload_bytes,
        }

    @mcp.custom_route(UPLOAD_PATH, methods=["POST", "PUT"])
    async def upload_file(request: Request) -> JSONResponse:
        # Rejecting here means no body is read without a valid token.
        token = request.headers.get(UPLOAD_TOKEN_HEADER, "")
        entry = pending.get(token)
        if entry is None or entry.expires_at <= time.monotonic():
            pending.pop(token, None)
            return JSONResponse(_REJECTED, status_code=404)

        # A refused upload keeps its token, so the caller can simply retry.
        user = entry.user_phid
        if (
            active.get(user, 0) >= MAX_ACTIVE_UPLOADS_PER_USER
            or sum(active.values()) >= MAX_ACTIVE_UPLOADS
        ):
            return JSONResponse(
                {
                    "success": False,
                    "error": "Too many uploads in progress; retry this request shortly",
                },
                status_code=429,
            )

        # Popping makes the token single use even when the upload then fails.
        # No await sits between the lookup above and the increment below, so
        # two requests cannot share a token or a slot. The slot is held until
        # the finally block, which runs only after the worker thread has
        # finished forwarding to Phabricator (the threadpool call is not
        # abandoned on cancellation).
        del pending[token]
        active[user] = active.get(user, 0) + 1
        try:
            return await _receive_and_store(request, entry)
        finally:
            active[user] -= 1
            if not active[user]:
                del active[user]

    async def _receive_and_store(
        request: Request, entry: _PendingUpload
    ) -> JSONResponse:
        too_large = JSONResponse(
            {
                "success": False,
                "error": f"File exceeds the {max_upload_bytes}-byte upload limit",
            },
            status_code=413,
        )
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_upload_bytes:
            return too_large

        async def read_body() -> Optional[bytearray]:
            body = bytearray()
            async for chunk in request.stream():
                body += chunk
                if len(body) > max_upload_bytes:
                    return None
            return body

        try:
            body = await asyncio.wait_for(read_body(), UPLOAD_BODY_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return JSONResponse(
                {"success": False, "error": "Timed out waiting for the file body"},
                status_code=408,
            )

        if body is None:
            return too_large

        if not body:
            return JSONResponse(
                {"success": False, "error": "Request body is empty"}, status_code=400
            )

        try:
            result = await run_in_threadpool(
                _store_file, entry.client, entry.name, body, view_policy
            )
        except PhabricatorAPIError as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=502)

        return JSONResponse(result)
