"""Free AI pictures (bot profile pictures and more) through Cloudflare Workers AI.

Workers AI's free plan includes a daily allowance (10,000 "neurons", roughly 50+ FLUX
images a day) and Cloudflare does not train on customer content without consent
(developers.cloudflare.com/workers-ai/platform/privacy). The user brings their own free
account id and API token; both live owner-only in ~/.zhunt/env.
"""

from __future__ import annotations

import base64
import os
import re

import httpx

PROVIDER = "Cloudflare Workers AI"
MODEL = "@cf/black-forest-labs/flux-1-schnell"
ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
KEY_URL = "https://dash.cloudflare.com/profile/api-tokens"
NOTE = (
    "Free daily allowance (about 50 pictures a day) with a free Cloudflare account. "
    "Create an API token with the 'Workers AI' permission and copy your account ID."
)
MAX_PROMPT = 500
_ACCOUNT = re.compile(r"[0-9a-f]{32}")
_TOKEN = re.compile(r"[\x21-\x7e]{20,200}")


class ImageError(RuntimeError):
    """Generation failed; the message is safe to show (credentials removed)."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def valid_account(value: str) -> bool:
    return bool(_ACCOUNT.fullmatch(value))


def valid_token(value: str) -> bool:
    return bool(_TOKEN.fullmatch(value))


def credentials() -> tuple[str, str] | None:
    account, token = os.environ.get(ACCOUNT_ENV, ""), os.environ.get(TOKEN_ENV, "")
    return (account, token) if account and token else None


async def generate(prompt: str, *, steps: int = 4, timeout: float = 90.0) -> bytes:
    """One JPEG for ``prompt``. Raises ImageError (409 when not connected)."""

    creds = credentials()
    if creds is None:
        raise ImageError("AI pictures use Cloudflare Workers AI: connect it in the Connect window", 409)
    account, token = creds
    prompt = prompt.strip()[:MAX_PROMPT]
    if not prompt:
        raise ImageError("describe the picture first", 400)
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{MODEL}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url, headers={"Authorization": f"Bearer {token}"}, json={"prompt": prompt, "steps": steps}
            )
    except httpx.HTTPError as error:
        raise ImageError(f"could not reach Cloudflare ({type(error).__name__})") from None
    if response.status_code == 429:
        raise ImageError("today's free picture allowance is used up; it resets daily", 429)
    if response.status_code in (401, 403):
        raise ImageError("Cloudflare rejected the token: check it has the Workers AI permission", 502)
    try:
        body = response.json()
    except ValueError:
        body = {}
    image = (body.get("result") or {}).get("image") if isinstance(body, dict) else None
    if response.status_code != 200 or not isinstance(image, str):
        errors = body.get("errors") if isinstance(body, dict) else None
        detail = (errors[0].get("message") if errors and isinstance(errors[0], dict) else "") or f"HTTP {response.status_code}"
        raise ImageError(f"Cloudflare could not make the picture: {detail.replace(token, '***')[:200]}")
    try:
        return base64.b64decode(image, validate=True)
    except ValueError:
        raise ImageError("Cloudflare returned an unreadable picture") from None
