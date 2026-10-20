# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP application for the iPad tech portal."""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.audit import AuditLog
from app.config import Config
from app.jamf_client import JamfClient
from app.jamf_url import JamfUrlError, normalize_jamf_url
from app.operations import TIMED_OUT, AppError, Portal
from app.privileges import feature_map
from app.redact import redact_text
from app.sessions import Session, SessionStore

STATIC_DIR = Path(__file__).resolve().parent / "static"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
LAUNCH_COOKIE = "itp_launch"
LAUNCH_COOKIE_SECONDS = 12 * 60 * 60
HTTPS_REQUIRED = "Sign-in is only available over HTTPS. Open the HTTPS address for this portal."
log = logging.getLogger("ipad")


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jamfUrl: str = Field(min_length=1, max_length=512)
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=256)


class JamfUrlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jamfUrl: str = Field(min_length=1, max_length=512)


class LocationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    departmentId: str = ""
    buildingId: str = ""
    room: str = ""
    updateInventory: bool = False


class UserBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = ""
    fullName: str = ""
    email: str = ""
    updateInventory: bool = False


class AssetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assetTag: str = ""
    updateInventory: bool = False


class GroupsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groupIds: list[str] = Field(default_factory=list, max_length=50)


class LostModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = ""
    phone: str = ""
    footnote: str = ""


class WipeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmSerial: str = ""
    wipeType: Literal["full", "return_to_service"] = "full"


def create_app(config: Config, jamf: JamfClient | None = None, launch_token: str | None = None) -> FastAPI:
    _setup_logging(config)
    client = jamf or JamfClient("", config.verify_tls, config.timeout_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not config.verify_tls:
            log.warning("Jamf TLS certificate verification is disabled.")
        if config.insecure_test_mode:
            log.warning("Insecure test mode is on. HTTP pages and HTTP Jamf URLs are accepted.")
        if config.ignored_jamf_url:
            log.warning(
                "jamf.url and ITP_JAMF_URL are ignored. Technicians enter the Jamf server on the sign-in page."
            )
        yield
        app.state.jamf.close()

    app = FastAPI(title="iPad Tech Portal", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.config = config
    app.state.jamf = client
    app.state.sessions = SessionStore(config.idle_timeout_seconds)
    app.state.launch_token = launch_token or ""
    app.state.loopback_exempt = config.loopback_exempt or bool(launch_token)
    app.state.audit = AuditLog(
        config.audit_path,
        config.audit_retention_days,
        config.audit_max_bytes,
        fallback_path=config.audit_fallback_path,
    )
    app.state.portal = Portal(config, client, app.state.audit)

    @app.exception_handler(RequestValidationError)
    async def invalid_body(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        # Do not echo the request body. A rejected login must not reflect the password.
        return JSONResponse(
            status_code=400,
            content={"error": "invalid", "message": "The request was not valid."},
        )

    @app.exception_handler(AppError)
    async def app_error(_request: Request, exc: AppError) -> JSONResponse:
        response = JSONResponse(status_code=exc.status, content={"error": exc.code, "message": exc.message})
        if exc.code == "session_expired":
            response.delete_cookie(config.cookie_name, path="/")
        return response

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if launch_token:
            if not _host_ok(request) or not _launch_presented_ok(request, launch_token):
                return _headers(_not_found())
            if request.method in {"GET", "HEAD"} and request.query_params.get("t"):
                response = RedirectResponse(_without_launch_query(request), status_code=303)
                _set_launch_cookie(response, config, launch_token)
                return _headers(response)

        if request.method == "POST" and request.url.path in {"/api/login", "/api/jamf-url/preview"}:
            if _https_blocked(request):
                return _headers(
                    JSONResponse(
                        status_code=403,
                        content={"error": "https_required", "message": HTTPS_REQUIRED},
                    )
                )

        store: SessionStore = request.app.state.sessions
        if request.method in UNSAFE and request.url.path.startswith("/api/"):
            status, session = store.lookup(request.cookies.get(config.cookie_name))
            if status == "expired":
                response = JSONResponse(
                    status_code=401,
                    content={"error": "session_expired", "message": TIMED_OUT},
                )
                response.delete_cookie(config.cookie_name, path="/")
                return _headers(response)
            header = request.headers.get("x-csrf-token", "")
            if session is None or not _csrf_ok(session.csrf_token, header):
                return _headers(
                    JSONResponse(
                        status_code=403,
                        content={
                            "error": "csrf",
                            "message": "The page security token was missing or expired. Reload and try again.",
                        },
                    )
                )
        response = await call_next(request)
        status, session = store.lookup(request.cookies.get(config.cookie_name))
        if status == "ok" and session is not None and request.url.path.startswith("/api/"):
            _set_cookie(response, config, session, request)
        response = _headers(response)
        if launch_token:
            _set_launch_cookie(response, config, launch_token)
        warning = request.app.state.audit.warning
        if warning:
            response.headers["X-Audit-Warning"] = warning
        return response

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/bootstrap")
    def bootstrap(request: Request) -> JSONResponse:
        store: SessionStore = request.app.state.sessions
        status, session = store.lookup(request.cookies.get(config.cookie_name))
        expired = status == "expired"
        if session is None:
            session = store.create_anonymous()
        else:
            store.touch(session)
        response = JSONResponse(
            _session_payload(session, config, request, expired=expired, audit_warning=request.app.state.audit.warning)
        )
        _set_cookie(response, config, session, request)
        return response

    @app.post("/api/jamf-url/preview")
    def preview_jamf_url(body: JamfUrlBody, request: Request) -> dict[str, str]:
        """Show the normalized hostname. The address is not stored."""
        store: SessionStore = request.app.state.sessions
        _status, session = store.lookup(request.cookies.get(config.cookie_name))
        if session is None:
            raise AppError(403, "csrf", "The page security token was missing or expired. Reload and try again.")
        _url, host = _checked_jamf_url(body.jamfUrl, config)
        return {"host": host}

    @app.post("/api/login")
    def login(body: LoginBody, request: Request) -> JSONResponse:
        store: SessionStore = request.app.state.sessions
        _status, session = store.lookup(request.cookies.get(config.cookie_name))
        if session is None:
            raise AppError(403, "csrf", "The page security token was missing or expired. Reload and try again.")
        jamf_url, jamf_host = _checked_jamf_url(body.jamfUrl, config)
        token, expires, username, privilege_set, privileges = request.app.state.portal.authenticate(
            jamf_url,
            jamf_host,
            body.username,
            body.password,
        )
        session.username = username
        session.token = token
        session.token_expires = expires
        session.privileges = privileges
        session.privilege_set = privilege_set
        session.jamf_url = jamf_url
        session.jamf_host = jamf_host
        store.rotate(session)
        response = JSONResponse(
            _session_payload(session, config, request, expired=False, audit_warning=request.app.state.audit.warning)
        )
        _set_cookie(response, config, session, request)
        return response

    @app.post("/api/logout")
    def logout(request: Request) -> JSONResponse:
        session = _require(request)
        request.app.state.portal.logout(session)
        request.app.state.sessions.delete(session.sid)
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(config.cookie_name, path="/")
        return response

    @app.get("/api/departments")
    def departments(request: Request) -> dict[str, Any]:
        return {"departments": request.app.state.portal.departments(_require(request))}

    @app.get("/api/buildings")
    def buildings(request: Request) -> dict[str, Any]:
        return {"buildings": request.app.state.portal.buildings(_require(request))}

    @app.get("/api/groups")
    def groups(request: Request) -> dict[str, Any]:
        return {"groups": request.app.state.portal.groups(_require(request))}

    @app.get("/api/devices")
    def find_devices(request: Request, q: str = "") -> dict[str, Any]:
        return request.app.state.portal.lookup(_require(request), q)

    @app.get("/api/devices/{device_id}")
    def read_device(device_id: str, request: Request) -> dict[str, Any]:
        return {"device": request.app.state.portal.get_device(_require(request), device_id)}

    @app.patch("/api/devices/{device_id}/location")
    def patch_location(device_id: str, body: LocationBody, request: Request) -> dict[str, Any]:
        return request.app.state.portal.update_location(
            _require(request),
            device_id,
            body.departmentId,
            body.buildingId,
            body.room,
            body.updateInventory,
        )

    @app.patch("/api/devices/{device_id}/user")
    def patch_user(device_id: str, body: UserBody, request: Request) -> dict[str, Any]:
        return request.app.state.portal.update_user(
            _require(request),
            device_id,
            body.username,
            body.fullName,
            body.email,
            body.updateInventory,
        )

    @app.patch("/api/devices/{device_id}/asset-tag")
    def patch_asset(device_id: str, body: AssetBody, request: Request) -> dict[str, Any]:
        return request.app.state.portal.update_asset_tag(
            _require(request),
            device_id,
            body.assetTag,
            body.updateInventory,
        )

    @app.post("/api/devices/{device_id}/groups")
    def post_groups(device_id: str, body: GroupsBody, request: Request) -> dict[str, Any]:
        return request.app.state.portal.update_groups(_require(request), device_id, body.groupIds)

    @app.post("/api/devices/{device_id}/commands/restart")
    def post_restart(device_id: str, request: Request) -> dict[str, Any]:
        return request.app.state.portal.restart(_require(request), device_id)

    @app.post("/api/devices/{device_id}/commands/update-inventory")
    def post_inventory(device_id: str, request: Request) -> dict[str, Any]:
        return request.app.state.portal.update_inventory(_require(request), device_id)

    @app.post("/api/devices/{device_id}/commands/clear-passcode")
    def post_passcode(device_id: str, request: Request) -> dict[str, Any]:
        return request.app.state.portal.clear_passcode(_require(request), device_id)

    @app.post("/api/devices/{device_id}/commands/lost-mode/enable")
    def post_lost_on(device_id: str, body: LostModeBody, request: Request) -> dict[str, Any]:
        return request.app.state.portal.enable_lost_mode(
            _require(request),
            device_id,
            body.message,
            body.phone,
            body.footnote,
        )

    @app.post("/api/devices/{device_id}/commands/lost-mode/disable")
    def post_lost_off(device_id: str, request: Request) -> dict[str, Any]:
        return request.app.state.portal.disable_lost_mode(_require(request), device_id)

    @app.post("/api/devices/{device_id}/commands/erase")
    def post_erase(device_id: str, body: WipeBody, request: Request) -> dict[str, Any]:
        return request.app.state.portal.erase(_require(request), device_id, body.confirmSerial, body.wipeType)

    @app.get("/")
    def index() -> HTMLResponse:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        if config.insecure_test_mode:
            html = html.replace(
                '<p id="insecure-banner" class="insecure-banner" hidden>',
                '<p id="insecure-banner" class="insecure-banner">',
                1,
            )
        return HTMLResponse(html)

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _require(request: Request) -> Session:
    store: SessionStore = request.app.state.sessions
    config: Config = request.app.state.config
    status, session = store.lookup(request.cookies.get(config.cookie_name))
    if status == "expired":
        raise AppError(401, "session_expired", TIMED_OUT)
    if session is None or not session.authenticated:
        raise AppError(401, "unauthenticated", "Sign in with your Jamf account.")
    store.touch(session)
    return session


def _session_payload(
    session: Session,
    config: Config,
    request: Request,
    expired: bool,
    audit_warning: str = "",
) -> dict[str, Any]:
    authenticated = session.authenticated
    features = feature_map(session.privileges, session.privilege_set) if authenticated else {}
    page_https = _page_is_https(request)
    page_required = _page_https_required(request)
    return {
        "authenticated": authenticated,
        "username": session.username if authenticated else "",
        "jamfHost": session.jamf_host if authenticated else "",
        "csrfToken": session.csrf_token,
        "idleTimeoutMinutes": config.idle_timeout_seconds // 60,
        "updateInventoryDefault": config.update_inventory_after_change,
        "features": features,
        "expired": expired,
        "auditWarning": audit_warning,
        "insecureTestMode": config.insecure_test_mode,
        "pageHttpsRequired": page_required,
        "pageIsHttps": page_https,
        "httpsRequiredMessage": HTTPS_REQUIRED if page_required and not page_https else "",
    }


def _set_cookie(response: Response, config: Config, session: Session, request: Request) -> None:
    response.set_cookie(
        config.cookie_name,
        session.sid,
        httponly=True,
        secure=_cookie_secure(config, request),
        samesite="strict",
        path="/",
        max_age=config.idle_timeout_seconds,
    )


def _cookie_secure(config: Config, request: Request) -> bool:
    if not config.cookie_secure:
        return False
    # A Secure cookie is not stored on an HTTP page. Test mode is the only
    # web path that allows that page, so the lab session has to be able to stick.
    if config.insecure_test_mode and not _page_is_https(request):
        return False
    return True


def _checked_jamf_url(raw: str, config: Config) -> tuple[str, str]:
    try:
        return normalize_jamf_url(raw, allow_http=config.insecure_test_mode)
    except JamfUrlError as exc:
        raise AppError(400, "invalid", exc.message) from None


def _page_https_required(request: Request) -> bool:
    config: Config = request.app.state.config
    if config.insecure_test_mode or request.app.state.loopback_exempt:
        return False
    return True


def _https_blocked(request: Request) -> bool:
    return _page_https_required(request) and not _page_is_https(request)


def _page_is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded.strip():
        return forwarded.split(",")[0].strip().casefold() == "https"
    return request.url.scheme == "https"


def _headers(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; "
        "img-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            record.args = tuple(redact_text(arg) if isinstance(arg, str) else arg for arg in record.args)
        return True


def _setup_logging(config: Config) -> None:
    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ipad")
    logger.setLevel(config.log_level)
    logger.handlers.clear()
    handler = logging.FileHandler(config.log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(_RedactFilter())
    logger.addHandler(handler)
    logger.propagate = False
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "not_found", "message": "Not found."})


def _host_ok(request: Request) -> bool:
    host = request.headers.get("host", "")
    hostname = host.split(":")[0].strip("[]").lower()
    return hostname in {"127.0.0.1", "localhost"}


def _launch_presented_ok(request: Request, expected: str) -> bool:
    query = request.query_params.get("t", "")
    if query and _csrf_ok(expected, query):
        return True
    cookie = request.cookies.get(LAUNCH_COOKIE, "")
    return bool(cookie) and _csrf_ok(expected, cookie)


def _without_launch_query(request: Request) -> str:
    cleaned = request.url.remove_query_params("t")
    if cleaned.query:
        return f"{cleaned.path}?{cleaned.query}"
    return cleaned.path


def _set_launch_cookie(response: Response, config: Config, token: str) -> None:
    response.set_cookie(
        LAUNCH_COOKIE,
        token,
        httponly=True,
        secure=config.cookie_secure,
        samesite="strict",
        path="/",
        max_age=LAUNCH_COOKIE_SECONDS,
    )


def _csrf_ok(expected: str, provided: str) -> bool:
    if not provided or len(provided) != len(expected):
        return False
    try:
        return hmac.compare_digest(expected, provided)
    except (TypeError, ValueError):
        return False
