"""A small Jamf Pro stand-in for local demos and tests.

Demo accounts exist only in this process:
  tech / demo-tech  — privileges for every portal action
  desk / demo-desk  — same, without Erase Device or Enable Lost Mode
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.privileges import TECH_PRIVILEGES

_EQ = re.compile(r'^([A-Za-z0-9]+)==(?:"((?:\\.|[^"])*)"|(.+))$')


@dataclass
class Device:
    id: str
    name: str
    model: str
    model_identifier: str
    os_version: str
    serial: str
    asset_tag: str
    username: str
    full_name: str
    email: str
    department_id: str
    building_id: str
    room: str
    last_inventory: str
    managed: bool
    supervised: bool
    lost_mode: bool
    management_id: str


@dataclass
class Store:
    users: dict[str, dict] = field(default_factory=dict)
    tokens: dict[str, dict] = field(default_factory=dict)
    devices: dict[str, Device] = field(default_factory=dict)
    departments: list[dict[str, str]] = field(default_factory=list)
    buildings: list[dict[str, str]] = field(default_factory=list)
    groups: list[dict] = field(default_factory=list)
    membership: dict[str, set[str]] = field(default_factory=dict)
    commands: list[dict] = field(default_factory=list)
    requests: list[tuple[str, str]] = field(default_factory=list)
    failures: dict[tuple[str, str], int] = field(default_factory=dict)
    classic_enabled: bool = True
    # Simulate Jamf Pro 11.30 rejecting v2 MDM commands: map commandType -> (status, description).
    mdm_failures: dict[str, tuple[int, str]] = field(default_factory=dict)
    # Force the Classic UpdateInventory command to fail: (status, html/xml body).
    classic_command_failure: tuple[int, str] | None = None


def build_store() -> Store:
    store = Store()
    desk = set(TECH_PRIVILEGES) - {
        "Send Mobile Device Remote Wipe Command",
        "Send Mobile Device Enable Lost Mode Command",
    }
    store.users = {
        "tech": {"password": "demo-tech", "privileges": set(TECH_PRIVILEGES), "privilege_set": "CUSTOM"},
        "desk": {"password": "demo-desk", "privileges": desk, "privilege_set": "CUSTOM"},
    }
    store.departments = [
        {"id": "1", "name": "Science"},
        {"id": "2", "name": "Mathematics"},
        {"id": "3", "name": "Art"},
    ]
    store.buildings = [
        {"id": "1", "name": "North Hall"},
        {"id": "2", "name": "South Hall"},
    ]
    store.groups = [
        {"groupId": "1", "groupName": "Tech-Loaner", "smart": False},
        {"groupId": "2", "groupName": "Tech-Repair", "smart": False},
        {"groupId": "3", "groupName": "All Supervised iPads", "smart": True},
        {"groupId": "4", "groupName": "Finance-Locked", "smart": False},
    ]
    store.devices = {
        "101": Device(
            "101", "Cart iPad 12", "iPad (10th generation)", "iPad13,18", "17.6.1",
            "DMPX2TEST001", "IPAD-1001", "ada", "Ada Lovelace", "ada@example.com",
            "1", "1", "12", "2026-10-01T15:04:00Z", True, True, False,
            "11111111-1111-1111-1111-111111111111",
        ),
        "102": Device(
            "102", "Cart iPad 13", "iPad Air (5th generation)", "iPad13,16", "18.0",
            "DMPX2TEST002", "IPAD-1002", "", "", "",
            "2", "2", "4", "2026-09-12T08:30:00Z", True, True, False,
            "22222222-2222-2222-2222-222222222222",
        ),
        "103": Device(
            "103", "Library iPad", "iPad mini (6th generation)", "iPad14,1", "17.5",
            "DMPX2TEST003", "IPAD-1001", "ben", "Ben Bibli", "ben@example.com",
            "3", "1", "1", "2026-08-02T11:00:00Z", True, True, False,
            "33333333-3333-3333-3333-333333333333",
        ),
        "104": Device(
            "104", "Front Desk Phone", "iPhone 14", "iPhone14,7", "18.0",
            "DMPXPHONE001", "PHONE-1", "", "", "",
            "1", "1", "1", "2026-07-01T00:00:00Z", True, True, False,
            "44444444-4444-4444-4444-444444444444",
        ),
        "105": Device(
            "105", "Spare iPad", "iPad (9th generation)", "iPad12,1", "16.7",
            "DMPX2TEST004", "IPAD-1004", "", "", "",
            "", "", "", "", False, False, False, "",
        ),
    }
    store.membership = {
        "1": {"101"},
        "2": {"103"},
        "3": {"101", "102", "103"},
        "4": {"101"},
    }
    return store


def create_mock_app() -> FastAPI:
    app = FastAPI()
    app.state.store = build_store()

    @app.middleware("http")
    async def record(request: Request, call_next):
        request.app.state.store.requests.append((request.method, request.url.path))
        forced = request.app.state.store.failures.pop((request.method, request.url.path), None)
        if forced is not None:
            return JSONResponse(status_code=forced, content={"httpStatus": forced, "errors": [{"description": "Forced failure"}]})
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/auth/token")
    async def token(request: Request) -> JSONResponse:
        store: Store = request.app.state.store
        username, password = _basic(request)
        user = store.users.get(username)
        if user is None or password != user["password"]:
            return _status(401)
        return _issue(store, username)

    @app.post("/api/v1/auth/keep-alive")
    def keep_alive(request: Request) -> JSONResponse:
        store: Store = request.app.state.store
        record = _token_record(store, request)
        if record is None:
            return _status(401)
        username = record["username"]
        store.tokens.pop(_bearer(request), None)
        return _issue(store, username)

    @app.post("/api/v1/auth/invalidate-token")
    def invalidate(request: Request) -> Response:
        store: Store = request.app.state.store
        store.tokens.pop(_bearer(request), None)
        return Response(status_code=204)

    @app.get("/api/v1/auth")
    def auth_details(request: Request) -> JSONResponse:
        user = _require_user(request)
        if isinstance(user, JSONResponse):
            return user
        return JSONResponse({
            "authenticationType": "LDAP",
            "account": {
                "username": user["username"],
                "privilegeSet": user["privilege_set"],
                "privilegesBySite": {"-1": sorted(user["privileges"])},
            },
        })

    @app.get("/api/v2/mobile-devices/detail")
    def detail(request: Request) -> JSONResponse:
        blocked = _need(request, "Read Mobile Devices")
        if blocked:
            return blocked
        store: Store = request.app.state.store
        parsed = _parse_eq(request.query_params.get("filter", ""))
        matched = list(store.devices.values())
        if parsed:
            field, value = parsed
            matched = [device for device in matched if _device_field(device, field) == value]
        return _page(request, [_device_json(store, device) for device in matched])

    @app.patch("/api/v2/mobile-devices/{device_id}")
    async def patch_device(device_id: str, request: Request) -> JSONResponse:
        blocked = _need(request, "Update Mobile Devices")
        if blocked:
            return blocked
        store: Store = request.app.state.store
        device = store.devices.get(device_id)
        if device is None:
            return _status(404)
        body = await request.json()
        if "assetTag" in body:
            device.asset_tag = str(body.get("assetTag") or "")
        location = body.get("location") or {}
        if "username" in location:
            device.username = str(location.get("username") or "")
        if "realName" in location:
            device.full_name = str(location.get("realName") or "")
        if "emailAddress" in location:
            device.email = str(location.get("emailAddress") or "")
        if "departmentId" in location:
            device.department_id = str(location.get("departmentId") or "")
        if "buildingId" in location:
            device.building_id = str(location.get("buildingId") or "")
        if "room" in location:
            device.room = str(location.get("room") or "")
        return JSONResponse(_device_json(store, device))

    @app.get("/api/v1/departments")
    def departments(request: Request) -> JSONResponse:
        blocked = _need(request, "Read Departments")
        if blocked:
            return blocked
        return _page(request, request.app.state.store.departments)

    @app.get("/api/v1/buildings")
    def buildings(request: Request) -> JSONResponse:
        blocked = _need(request, "Read Buildings")
        if blocked:
            return blocked
        return _page(request, request.app.state.store.buildings)

    @app.get("/api/v2/mobile-device-groups/static-groups")
    @app.get("/api/v1/mobile-device-groups/static-groups")
    def static_groups(request: Request) -> JSONResponse:
        blocked = _need(request, "Read Static Mobile Device Groups")
        if blocked:
            return blocked
        rows = [
            {"groupId": group["groupId"], "groupName": group["groupName"], "siteId": "-1"}
            for group in request.app.state.store.groups
            if not group["smart"]
        ]
        return _page(request, rows)

    @app.get("/JSSResource/mobiledevices/id/{device_id}/subset/MobileDeviceGroups")
    def classic_groups(device_id: str, request: Request) -> JSONResponse:
        store: Store = request.app.state.store
        if not store.classic_enabled:
            return _status(404)
        blocked = _need(request, "Read Mobile Devices")
        if blocked:
            return blocked
        if device_id not in store.devices:
            return _status(404)
        groups = []
        for group in store.groups:
            if device_id in store.membership.get(group["groupId"], set()):
                groups.append({"id": int(group["groupId"]), "name": group["groupName"], "is_smart": group["smart"]})
        payload: dict | list = groups
        if len(groups) == 1:
            payload = groups[0]
        return JSONResponse({
            "mobile_device": {
                "mobile_device_groups": {"mobile_device_group": payload} if groups else {}
            }
        })

    @app.get("/api/v2/mobile-device-groups/static-group-membership/{group_id}")
    def membership(group_id: str, request: Request) -> JSONResponse:
        blocked = _need(request, "Read Static Mobile Device Groups")
        if blocked:
            return blocked
        store: Store = request.app.state.store
        if not any(group["groupId"] == group_id and not group["smart"] for group in store.groups):
            return _status(404)
        parsed = _parse_eq(request.query_params.get("filter", ""))
        members = []
        for device_id in sorted(store.membership.get(group_id, set())):
            device = store.devices.get(device_id)
            if device is None:
                continue
            if parsed and parsed[0] == "mobileDeviceId" and device.id != parsed[1]:
                continue
            members.append({"mobileDeviceId": device.id, "serialNumber": device.serial})
        return _page(request, members)

    @app.patch("/api/v2/mobile-device-groups/static-groups/{group_id}")
    async def patch_group(group_id: str, request: Request) -> JSONResponse:
        blocked = _need(request, "Update Static Mobile Device Groups")
        if blocked:
            return blocked
        store: Store = request.app.state.store
        group = next((item for item in store.groups if item["groupId"] == group_id and not item["smart"]), None)
        if group is None:
            return _status(404)
        body = await request.json()
        members = store.membership.setdefault(group_id, set())
        for assignment in body.get("assignments") or []:
            device_id = str(assignment.get("mobileDeviceId") or "")
            if device_id not in store.devices:
                return JSONResponse(
                    status_code=400,
                    content={"httpStatus": 400, "errors": [{"description": "Unknown mobile device"}]},
                )
            if assignment.get("selected"):
                members.add(device_id)
            else:
                members.discard(device_id)
        return JSONResponse({"groupId": group_id, "groupName": group["groupName"]})

    @app.post("/api/v2/mdm/commands")
    async def mdm(request: Request) -> JSONResponse:
        user = _require_user(request)
        if isinstance(user, JSONResponse):
            return user
        if "View MDM command information in Jamf Pro API" not in user["privileges"]:
            return _status(403)
        body = await request.json()
        command = body.get("commandData") or {}
        command_type = str(command.get("commandType") or "")
        needed = {
            "RESTART_DEVICE": "Send Mobile Device Restart Device Command",
            "DEVICE_INFORMATION": "Send Mobile Device Update Inventory Command",
            "CLEAR_PASSCODE": "Send Mobile Device Clear Passcode Command",
            "ENABLE_LOST_MODE": "Send Mobile Device Enable Lost Mode Command",
            "DISABLE_LOST_MODE": "Send Mobile Device Disable Lost Mode Command",
            "ERASE_DEVICE": "Send Mobile Device Remote Wipe Command",
        }.get(command_type)
        if needed is None:
            return JSONResponse(status_code=400, content={"httpStatus": 400, "errors": [{"description": "Unknown command"}]})
        if needed not in user["privileges"]:
            return _status(403)
        forced_mdm = request.app.state.store.mdm_failures.get(command_type)
        if forced_mdm is not None:
            code, description = forced_mdm
            return JSONResponse(status_code=code, content={"httpStatus": code, "errors": [{"code": "UNKNOWN", "description": description}]})
        clients = body.get("clientData") or []
        management_id = str(clients[0].get("managementId") or "") if clients else ""
        device = next((item for item in request.app.state.store.devices.values() if item.management_id == management_id), None)
        if device is None:
            return _status(404)
        if command_type == "ENABLE_LOST_MODE":
            if not command.get("lostModeMessage") and not command.get("lostModePhone"):
                return JSONResponse(
                    status_code=400,
                    content={"httpStatus": 400, "errors": [{"description": "Lost Mode needs a message or a phone number."}]},
                )
            device.lost_mode = True
        elif command_type == "DISABLE_LOST_MODE":
            device.lost_mode = False
        command_id = str(len(request.app.state.store.commands) + 1)
        request.app.state.store.commands.append({
            "id": command_id,
            "commandType": command_type,
            "managementId": management_id,
            "username": user["username"],
        })
        return JSONResponse(status_code=201, content={"id": command_id, "commandUuid": command_id})

    @app.post("/JSSResource/mobiledevicecommands/command/UpdateInventory/id/{device_id}")
    def classic_update_inventory(device_id: str, request: Request) -> Response:
        store: Store = request.app.state.store
        if not store.classic_enabled:
            return _status(404)
        user = _require_user(request)
        if isinstance(user, JSONResponse):
            return user
        if "Send Mobile Device Update Inventory Command" not in user["privileges"]:
            return _status(403)
        if store.classic_command_failure is not None:
            code, text = store.classic_command_failure
            return Response(status_code=code, content=f"<html><body><p>{text}</p></body></html>", media_type="text/html")
        device = store.devices.get(device_id)
        if device is None or not device_id.isdigit():
            return Response(status_code=404, content="<html><body><p>The server has not found anything matching the request URI</p></body></html>", media_type="text/html")
        command_id = str(len(store.commands) + 1)
        store.commands.append({
            "id": command_id,
            "commandType": "DEVICE_INFORMATION",
            "managementId": device.management_id,
            "username": user["username"],
            "api": "classic",
        })
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?><mobile_device_command>'
            f"<command>UpdateInventory</command><mobile_devices><mobile_device><id>{device_id}</id>"
            "<status/></mobile_device></mobile_devices></mobile_device_command>"
        )
        return Response(status_code=201, content=xml, media_type="application/xml")

    return app


def _issue(store: Store, username: str) -> JSONResponse:
    token = secrets.token_urlsafe(24)
    expires = datetime.now(timezone.utc) + timedelta(minutes=30)
    store.tokens[token] = {"username": username, "expires": expires}
    return JSONResponse({"token": token, "expires": expires.strftime("%Y-%m-%dT%H:%M:%SZ")})


def _basic(request: Request) -> tuple[str, str]:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return "", ""
    import base64

    try:
        decoded = base64.b64decode(header.split(" ", 1)[1].strip()).decode("utf-8")
    except (ValueError, UnicodeError):
        return "", ""
    username, separator, password = decoded.partition(":")
    if not separator:
        return "", ""
    return username, password


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return ""
    return header.split(" ", 1)[1].strip()


def _token_record(store: Store, request: Request) -> dict | None:
    return store.tokens.get(_bearer(request))


def _require_user(request: Request) -> dict | JSONResponse:
    store: Store = request.app.state.store
    record = _token_record(store, request)
    if record is None:
        return _status(401)
    user = store.users[record["username"]]
    return {"username": record["username"], **user}


def _need(request: Request, privilege: str) -> JSONResponse | None:
    user = _require_user(request)
    if isinstance(user, JSONResponse):
        return user
    if privilege not in user["privileges"]:
        return _status(403)
    return None


def _status(code: int) -> JSONResponse:
    return JSONResponse(status_code=code, content={"httpStatus": code})


def _page(request: Request, rows: list) -> JSONResponse:
    try:
        page = int(request.query_params.get("page") or 0)
        size = int(request.query_params.get("page-size") or 100)
    except ValueError:
        page, size = 0, 100
    start = max(page, 0) * max(size, 1)
    return JSONResponse({"totalCount": len(rows), "results": rows[start:start + size]})


def _device_json(store: Store, device: Device) -> dict:
    department = next((item["name"] for item in store.departments if item["id"] == device.department_id), "")
    building = next((item["name"] for item in store.buildings if item["id"] == device.building_id), "")
    return {
        "mobileDeviceId": device.id,
        "deviceType": "iOS" if "ipad" in device.model.casefold() or "iphone" in device.model.casefold() else "iOS",
        "general": {
            "displayName": device.name,
            "assetTag": device.asset_tag,
            "osVersion": device.os_version,
            "managed": device.managed,
            "supervised": device.supervised,
            "managementId": device.management_id,
            "lastInventoryUpdateDate": device.last_inventory,
        },
        "hardware": {
            "serialNumber": device.serial,
            "model": device.model,
            "modelIdentifier": device.model_identifier,
        },
        "userAndLocation": {
            "username": device.username,
            "realName": device.full_name,
            "emailAddress": device.email,
            "departmentId": device.department_id,
            "department": department,
            "buildingId": device.building_id,
            "building": building,
            "room": device.room,
        },
        "security": {"lostModeEnabled": device.lost_mode},
    }


def _device_field(device: Device, field_name: str) -> str:
    return {
        "serialNumber": device.serial,
        "assetTag": device.asset_tag,
        "mobileDeviceId": device.id,
    }.get(field_name, "")


def _parse_eq(expression: str) -> tuple[str, str] | None:
    match = _EQ.match(expression or "")
    if not match:
        return None
    raw = match.group(2) if match.group(2) is not None else match.group(3)
    return match.group(1), _unescape(raw)


def _unescape(text: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            output.append(text[index + 1])
            index += 2
        else:
            output.append(text[index])
            index += 1
    return "".join(output)
