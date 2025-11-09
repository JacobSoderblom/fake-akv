import os

from fastapi import Request


def akv_base_url(request: Request) -> str:
    base = os.getenv("FAKE_AKV_BASE_URL")
    if base:
        return base.rstrip("/")

    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.hostname
    )
    return f"{scheme}://{host}".rstrip("/")
