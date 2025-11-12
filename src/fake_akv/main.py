import os
import time
from typing import Any, Optional

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .models import DeletedSecretResult, SecretCreate, SecretResult
from .storage import Storage
from .utils import akv_base_url

API_VERSION = "7.4"

app = FastAPI(title="Fake Azure Key Vault (Secrets)")
storage = Storage()


SUPPORTED_API_VERSIONS = {
    "7.2",
    "7.3",
    "7.4",
    "7.5",
    "7.5-preview",
    "7.6",
    "7.6-preview",
}


def require_api_version(request: Request):
    v = request.query_params.get("api-version")
    if v is None:
        return
    if v in SUPPORTED_API_VERSIONS or v.startswith("7."):
        return
    raise HTTPException(status_code=400, detail=f"Unsupported api-version '{v}'")


def require_auth(request: Request, authorization: Optional[str]):
    if os.getenv("FAKE_AKV_REQUIRE_AUTH", "true").lower() != "true":
        return
    if not authorization or not authorization.startswith("Bearer "):
        host = request.headers.get("host") or request.url.netloc
        host_only = host.split(":")[0]
        base = f"{request.url.scheme}://{host_only}"
        challenge = (
            "Bearer "
            'authorization="https://login.windows.net/00000000-0000-0000-0000-000000000000", '
            f'resource="{base}", scope="{base}/.default"'
        )
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": challenge},
        )


def _unix_now() -> int:
    return int(time.time())


def build_secret_result(
    request: Request,
    name: str,
    version: Optional[str],
    data: dict[str, Any],
    include_value: bool = True,
) -> SecretResult:
    base = akv_base_url(request)

    attrs: dict[str, Any] = {
        "enabled": data.get("enabled", True),
        "created": data.get("created") or _unix_now(),
        "updated": data.get("updated") or _unix_now(),
        "recoveryLevel": (data.get("recoveryLevel") or "Purgeable"),
    }
    extra_attrs = data.get("attributes") or {}
    if extra_attrs:
        attrs.update(extra_attrs)

    body = SecretResult(
        value=str(data.get("value"))
        if include_value and data.get("value") is not None
        else None,
        id=f"{base}/secrets/{name}/{version or '00000000000000000000000000000000'}",
        attributes=attrs,
        tags=data.get("tags")
        if isinstance(data.get("tags"), dict) and data["tags"]
        else None,
    )
    return body


def build_secret_properties_result(
    request: Request, name: str, data: dict[str, Any]
) -> dict[str, Any]:
    base = akv_base_url(request)
    attrs: dict[str, Any] = {
        "enabled": data.get("enabled", True),
        "created": data.get("created") or _unix_now(),
        "updated": data.get("updated") or _unix_now(),
        "recoveryLevel": (data.get("recoveryLevel") or "Purgeable"),
    }
    extra_attrs = data.get("attributes") or {}
    if extra_attrs:
        attrs.update(extra_attrs)

    payload: dict[str, Any] = {
        "id": f"{base}/secrets/{name}",
        "attributes": attrs,
    }
    # Optional fields: only include if present (not None)
    if isinstance(data.get("tags"), dict) and data["tags"]:
        payload["tags"] = {str(k): str(v) for k, v in data["tags"].items()}
    content_type = (data.get("attributes") or {}).get("contentType")
    if content_type is not None:
        payload["contentType"] = str(content_type)

    return payload


@app.put("/secrets/{name}")
async def put_secret(
    name: str,
    request: Request,
    payload: Optional[SecretCreate] = Body(default=None),  # <-- now optional
    authorization: Optional[str] = Header(None),
):
    require_api_version(request)
    require_auth(request, authorization)

    body_dict: dict[str, Any] = {}

    if payload is not None:
        body_dict = payload.model_dump(exclude_none=True)
    else:
        try:
            raw = await request.json()
            if isinstance(raw, dict):
                body_dict = raw
        except Exception:
            pass

    if not body_dict:
        try:
            form = await request.form()
            if "value" in form:
                body_dict = {"value": form["value"]}
        except Exception:
            pass

    if not body_dict:
        qv = request.query_params.get("value")
        if qv is not None:
            body_dict = {"value": qv}

    if "value" not in body_dict or body_dict["value"] is None:
        raise HTTPException(status_code=400, detail="Request body must include 'value'")

    version, _ = storage.put_secret(
        name,
        body_dict["value"],
        body_dict.get("tags"),
        body_dict.get("attributes"),
    )
    data = storage.get_version(name, version)
    if data is None:
        raise HTTPException(status_code=500, detail="Secret creation failed")
    return JSONResponse(
        build_secret_result(request, name, version, data).model_dump(exclude_none=True)
    )


@app.get("/secrets/{name}")
async def get_secret_latest(
    name: str, request: Request, authorization: Optional[str] = Header(None)
):
    require_api_version(request)
    require_auth(request, authorization)
    res = storage.get_latest(name)
    if not res:
        raise HTTPException(status_code=404, detail="Secret not found")
    version, data = res
    return JSONResponse(
        build_secret_result(request, name, version, data).model_dump(exclude_none=True)
    )


@app.get("/secrets")
async def list_secrets(
    request: Request,
    authorization: Optional[str] = Header(None),
    maxresults: Optional[int] = Query(None, alias="maxresults"),
):
    """
    List secret properties (no values). Azure returns { value: [...], nextLink: null }.
    We accept optional 'maxresults' but return everything and nextLink=None for simplicity.
    """
    require_api_version(request)
    require_auth(request, authorization)

    items = []
    for name, data in storage.list_names_latest():
        items.append(build_secret_properties_result(request, name, data))

    if isinstance(maxresults, int) and maxresults > 0:
        items = items[:maxresults]

    return {"value": items, "nextLink": None}


@app.get("/secrets/{name}/versions")
async def list_secret_versions(
    name: str, request: Request, authorization: Optional[str] = Header(None)
):
    require_api_version(request)
    require_auth(request, authorization)
    items = []
    for version, data in storage.list_versions(name):
        items.append(
            build_secret_result(
                request, name, version, data, include_value=False
            ).model_dump(exclude_none=True)
        )
    return {"value": items, "nextLink": None}


@app.get("/secrets/{name}/{version}")
async def get_secret_version(
    name: str,
    version: str,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    require_api_version(request)
    require_auth(request, authorization)
    data = storage.get_version(name, version)
    if data is None:
        raise HTTPException(status_code=404, detail="Secret/version not found")
    return JSONResponse(
        build_secret_result(request, name, version, data).model_dump(exclude_none=True)
    )


@app.delete("/secrets/{name}")
async def delete_secret(
    name: str, request: Request, authorization: Optional[str] = Header(None)
):
    require_api_version(request)
    require_auth(request, authorization)
    deleted = storage.soft_delete(name)
    if not deleted:
        raise HTTPException(status_code=404, detail="Secret not found")
    base = akv_base_url(request)
    latest = storage.get_latest(name)
    version, data = latest if latest else ("", {"attributes": {}, "tags": {}})
    body = DeletedSecretResult(
        recoveryId=f"{base}/deletedsecrets/{name}",
        deletedDate=int(deleted["deletedDate"]),
        scheduledPurgeDate=int(deleted["scheduledPurgeDate"]),
        id=f"{base}/secrets/{name}/{version}",
        attributes=data.get("attributes") or {},
        tags=data.get("tags") or None,
    )
    return JSONResponse(body.model_dump(exclude_none=True))


@app.get("/deletedsecrets/{name}")
async def get_deleted_secret(
    name: str, request: Request, authorization: Optional[str] = Header(None)
):
    require_api_version(request)
    require_auth(request, authorization)
    deleted = storage.get_deleted(name)
    if not deleted:
        raise HTTPException(status_code=404, detail="Not deleted")
    base = akv_base_url(request)
    latest = storage.get_latest(name)
    version, data = latest if latest else ("", {"attributes": {}, "tags": {}})
    body = DeletedSecretResult(
        recoveryId=f"{base}/deletedsecrets/{name}",
        deletedDate=int(deleted["deletedDate"]),
        scheduledPurgeDate=int(deleted["scheduledPurgeDate"]),
        id=f"{base}/secrets/{name}/{version}",
        attributes=data.get("attributes") or {},
        tags=data.get("tags") or None,
    )
    return JSONResponse(body.model_dump(exclude_none=True))


@app.post("/deletedsecrets/{name}/recover")
async def recover_deleted_secret(
    name: str, request: Request, authorization: Optional[str] = Header(None)
):
    require_api_version(request)
    require_auth(request, authorization)
    ok = storage.recover(name)
    if not ok:
        raise HTTPException(status_code=404, detail="Nothing to recover")
    latest = storage.get_latest(name)
    version, data = latest if latest else ("", {"attributes": {}, "tags": {}})
    return JSONResponse(
        build_secret_result(request, name, version, data).model_dump(exclude_none=True)
    )


@app.get("/")
async def root():
    return {"name": "fake-akv", "status": "ok", "api-version": API_VERSION}
