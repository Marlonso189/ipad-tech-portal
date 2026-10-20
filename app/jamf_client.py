# SPDX-License-Identifier: AGPL-3.0-or-later
"""Jamf Pro HTTP client. The browser never calls Jamf itself."""

from __future__ import annotations

import base64
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import quote

import httpx


class JamfError(Exception):
    """Jamf could not complete a call."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class JamfAccessError(JamfError):
    """Jamf returned 401 or 403."""

    def __init__(self, status: int, action: str) -> None:
        super().__init__(f"Jamf returned {status} for {action}.")
        self.status = status
        self.action = action


class JamfHTTPError(JamfError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class JamfClient:
    def __init__(self, base_url: str, verify_tls: bool, timeout: float, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._local = threading.local()
        self._owns_client = client is None
        # No base_url on the client. Each sign-in supplies an absolute URL via use(),
        # so one process can talk to more than one Jamf server without keeping those
        # addresses outside the session that typed them.
        self._client = client or httpx.Client(
            verify=verify_tls,
            timeout=timeout,
            follow_redirects=False,
        )

    @contextmanager
    def use(self, base_url: str) -> Iterator[None]:
        """Send the enclosed calls to this Jamf origin. The address is not saved."""
        previous = getattr(self._local, "base_url", "")
        self._local.base_url = base_url.rstrip("/")
        try:
            yield
        finally:
            self._local.base_url = previous

    def _request_url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        base = getattr(self._local, "base_url", "") or self.base_url
        if not base:
            return path
        return base.rstrip("/") + "/" + path.lstrip("/")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def create_token(self, username: str, password: str) -> tuple[str, float]:
        # Basic auth is built here so the password is not passed through a
        # logging helper. The value is not stored after this call returns.
        basic = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        response = self._send(
            "POST",
            "/api/v1/auth/token",
            action="signing in",
            headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
        )
        return _parse_token(response)

    def keep_alive(self, token: str) -> tuple[str, float]:
        response = self._send(
            "POST",
            "/api/v1/auth/keep-alive",
            action="refreshing the Jamf session",
            token=token,
        )
        return _parse_token(response)

    def invalidate(self, token: str) -> None:
        try:
            self._send(
                "POST",
                "/api/v1/auth/invalidate-token",
                action="signing out",
                token=token,
                ok_status=(200, 204, 401),
            )
        except JamfError:
            # Logout still drops the local session if Jamf cannot be reached.
            return

    def current_auth(self, token: str) -> dict[str, Any]:
        response = self._send("GET", "/api/v1/auth", action="reading account privileges", token=token)
        if not isinstance(response, dict):
            raise JamfHTTPError(200, "Jamf returned an unexpected privileges payload.")
        return response

    def search_devices(self, token: str, field: str, value: str) -> list[dict[str, Any]]:
        if field not in {"serialNumber", "assetTag", "mobileDeviceId"}:
            raise JamfError("Unsupported device search field.")
        collected: list[dict[str, Any]] = []
        page = 0
        while page < 5:
            params = [
                ("section", "GENERAL"),
                ("section", "HARDWARE"),
                ("section", "USER_AND_LOCATION"),
                ("section", "SECURITY"),
                ("page", str(page)),
                ("page-size", "100"),
                ("filter", f"{field}=={_rsql_quote(value)}"),
            ]
            payload = self._send(
                "GET",
                "/api/v2/mobile-devices/detail",
                action="looking up iPads",
                token=token,
                params=params,
            )
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list) or not results:
                break
            collected.extend(item for item in results if isinstance(item, dict))
            total = payload.get("totalCount") if isinstance(payload, dict) else None
            if not isinstance(total, int) or len(collected) >= total or len(results) < 100:
                break
            page += 1
        return collected

    def update_device(self, token: str, device_id: str, body: dict[str, Any]) -> None:
        self._send(
            "PATCH",
            f"/api/v2/mobile-devices/{_path_id(device_id)}",
            action="updating the iPad",
            token=token,
            json_body=body,
        )

    def list_departments(self, token: str) -> list[dict[str, str]]:
        rows = self._pages("/api/v1/departments", token, action="reading departments")
        return _id_name_rows(rows)

    def list_buildings(self, token: str) -> list[dict[str, str]]:
        rows = self._pages("/api/v1/buildings", token, action="reading buildings")
        return _id_name_rows(rows)

    def list_static_groups(self, token: str) -> list[dict[str, str]]:
        try:
            rows = self._pages(
                "/api/v2/mobile-device-groups/static-groups",
                token,
                action="viewing static groups",
            )
        except JamfHTTPError as exc:
            if exc.status != 404:
                raise
            rows = self._pages(
                "/api/v1/mobile-device-groups/static-groups",
                token,
                action="viewing static groups",
            )
        groups: list[dict[str, str]] = []
        for row in rows:
            group_id = row.get("groupId", row.get("id"))
            name = row.get("groupName", row.get("name"))
            if group_id is None or not name:
                continue
            groups.append({"id": str(group_id), "name": str(name)})
        return groups

    def device_groups_classic(self, token: str, device_id: str) -> list[dict[str, Any]]:
        payload = self._send(
            "GET",
            f"/JSSResource/mobiledevices/id/{_path_id(device_id)}/subset/MobileDeviceGroups",
            action="viewing static groups",
            token=token,
        )
        return _classic_groups(payload)

    def static_group_contains(self, token: str, group_id: str, device_id: str) -> bool:
        params = [
            ("page", "0"),
            ("page-size", "1"),
            ("filter", f"mobileDeviceId=={_rsql_quote(device_id)}"),
        ]
        path = f"/api/v2/mobile-device-groups/static-group-membership/{_path_id(group_id)}"
        try:
            payload = self._send(
                "GET",
                path,
                action="viewing static groups",
                token=token,
                params=params,
            )
        except JamfHTTPError as exc:
            if exc.status != 404:
                raise
            payload = self._send(
                "GET",
                f"/api/v1/mobile-device-groups/static-group-membership/{_path_id(group_id)}",
                action="viewing static groups",
                token=token,
                params=params,
            )
        if not isinstance(payload, dict):
            return False
        total = payload.get("totalCount")
        if isinstance(total, int):
            return total > 0
        results = payload.get("results")
        return isinstance(results, list) and len(results) > 0

    def set_static_membership(self, token: str, group_id: str, device_id: str, selected: bool) -> None:
        body = {"assignments": [{"mobileDeviceId": str(device_id), "selected": selected}]}
        path = f"/api/v2/mobile-device-groups/static-groups/{_path_id(group_id)}"
        try:
            self._send(
                "PATCH",
                path,
                action="changing static group membership",
                token=token,
                json_body=body,
            )
        except JamfHTTPError as exc:
            if exc.status != 404:
                raise
            self._send(
                "PATCH",
                f"/api/v1/mobile-device-groups/static-groups/{_path_id(group_id)}",
                action="changing static group membership",
                token=token,
                json_body=body,
            )

    def send_mdm(self, token: str, management_id: str, command_data: dict[str, Any], action: str) -> dict[str, Any]:
        body = {
            "clientData": [{"managementId": management_id}],
            "commandData": command_data,
        }
        payload = self._send(
            "POST",
            "/api/v2/mdm/commands",
            action=action,
            token=token,
            json_body=body,
            ok_status=(200, 201),
        )
        return payload if isinstance(payload, dict) else {}

    def erase_return_to_service(self, token: str, device_id: str, action: str) -> dict[str, Any]:
        """Wipe with Return to Service using the PreStage enrollment's Wi-Fi profile."""
        payload = self._send(
            "POST",
            f"/api/v2/mobile-devices/{_path_id(device_id)}/erase",
            action=action,
            token=token,
            json_body={"returnToService": True},
            ok_status=(200, 201),
        )
        return payload if isinstance(payload, dict) else {}

    def classic_update_inventory(self, token: str, device_id: str) -> None:
        """Classic API fallback for Update Inventory, addressed by Jamf's numeric device id."""
        if not str(device_id).isdigit():
            raise JamfError("This iPad has no numeric Jamf device id, so the Classic API cannot be used.")
        path = f"/JSSResource/mobiledevicecommands/command/UpdateInventory/id/{device_id}"
        try:
            response = self._client.request(
                "POST",
                self._request_url(path),
                headers={"Accept": "application/xml", "Authorization": f"Bearer {token}"},
            )
        except httpx.TimeoutException as exc:
            raise JamfError("Jamf Pro did not respond in time.") from exc
        except httpx.HTTPError as exc:
            raise JamfError("Could not reach Jamf Pro.") from exc
        if response.status_code in (401, 403):
            raise JamfAccessError(response.status_code, "Update Inventory (Classic API)")
        if response.status_code not in (200, 201, 204):
            raise JamfHTTPError(response.status_code, _classic_error_message(response))

    def _pages(self, path: str, token: str, action: str) -> list[dict[str, Any]]:
        page = 0
        collected: list[dict[str, Any]] = []
        while page < 50:
            payload = self._send(
                "GET",
                path,
                action=action,
                token=token,
                params=[("page", str(page)), ("page-size", "200")],
            )
            if not isinstance(payload, dict):
                break
            results = payload.get("results")
            if not isinstance(results, list) or not results:
                break
            collected.extend(item for item in results if isinstance(item, dict))
            total = payload.get("totalCount")
            if isinstance(total, int) and len(collected) >= total:
                break
            if len(results) < 200:
                break
            page += 1
        return collected

    def _send(
        self,
        method: str,
        path: str,
        *,
        action: str,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        params: list[tuple[str, str]] | None = None,
        json_body: dict[str, Any] | None = None,
        ok_status: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        request_headers = {"Accept": "application/json"}
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        if headers:
            request_headers.update(headers)
        try:
            response = self._client.request(
                method,
                self._request_url(path),
                headers=request_headers,
                params=params,
                json=json_body,
            )
        except httpx.TimeoutException as exc:
            raise JamfError("Jamf Pro did not respond in time.") from exc
        except httpx.ConnectError as exc:
            raise JamfError("Could not reach Jamf Pro. Check the server URL and try again.") from exc
        except httpx.HTTPError as exc:
            raise JamfError("Could not reach Jamf Pro.") from exc

        if response.status_code in (401, 403):
            raise JamfAccessError(response.status_code, action)
        if response.status_code not in ok_status:
            raise JamfHTTPError(response.status_code, _error_message(response))
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise JamfHTTPError(response.status_code, "Jamf returned a response that was not JSON.") from exc


def parse_device(record: dict[str, Any]) -> dict[str, Any]:
    general = record.get("general") if isinstance(record.get("general"), dict) else {}
    hardware = record.get("hardware") if isinstance(record.get("hardware"), dict) else {}
    location = record.get("userAndLocation") if isinstance(record.get("userAndLocation"), dict) else {}
    security = record.get("security") if isinstance(record.get("security"), dict) else {}
    return {
        "id": str(record.get("mobileDeviceId") or record.get("id") or ""),
        "name": str(general.get("displayName") or ""),
        "model": str(hardware.get("model") or ""),
        "modelIdentifier": str(hardware.get("modelIdentifier") or ""),
        "osVersion": str(general.get("osVersion") or ""),
        "serialNumber": str(hardware.get("serialNumber") or ""),
        "assetTag": str(general.get("assetTag") or ""),
        "username": str(location.get("username") or ""),
        "fullName": str(location.get("realName") or location.get("fullName") or ""),
        "email": str(location.get("emailAddress") or ""),
        "department": str(location.get("department") or ""),
        "departmentId": str(location.get("departmentId") or ""),
        "building": str(location.get("building") or ""),
        "buildingId": str(location.get("buildingId") or ""),
        "room": str(location.get("room") or ""),
        "lastInventory": str(general.get("lastInventoryUpdateDate") or ""),
        "managed": bool(general.get("managed")),
        "supervised": bool(general.get("supervised")),
        "lostModeEnabled": bool(security.get("lostModeEnabled")),
        "managementId": str(general.get("managementId") or ""),
    }


def is_ipad(device: dict[str, Any]) -> bool:
    text = f"{device.get('model', '')} {device.get('modelIdentifier', '')}".casefold()
    return "ipad" in text


def _parse_token(payload: Any) -> tuple[str, float]:
    if not isinstance(payload, dict) or not payload.get("token"):
        raise JamfHTTPError(200, "Jamf did not return a bearer token.")
    return str(payload["token"]), _parse_expiry(payload.get("expires"))


def _parse_expiry(value: Any) -> float:
    if isinstance(value, (int, float)):
        # Jamf has used both epoch seconds and epoch milliseconds.
        return float(value / 1000) if value > 10_000_000_000 else float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return datetime.now(timezone.utc).timestamp() + 20 * 60
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return datetime.now(timezone.utc).timestamp() + 20 * 60


def _rsql_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _path_id(value: str) -> str:
    if not value or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in value):
        raise JamfError("That device or group id is not valid.")
    return quote(value, safe="")


def _id_name_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for row in rows:
        if row.get("id") is None or not row.get("name"):
            continue
        items.append({"id": str(row["id"]), "name": str(row["name"])})
    items.sort(key=lambda item: item["name"].casefold())
    return items


def _classic_groups(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    device = payload.get("mobile_device")
    if not isinstance(device, dict):
        return []
    container = device.get("mobile_device_groups")
    raw: Any
    if isinstance(container, dict):
        raw = container.get("mobile_device_group", [])
    elif isinstance(container, list):
        raw = container
    else:
        raw = []
    if isinstance(raw, dict):
        raw = [raw]
    groups: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return groups
    for item in raw:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        smart = item.get("is_smart", item.get("isSmart"))
        groups.append(
            {
                "id": str(item.get("id")),
                "name": str(item.get("name") or ""),
                "smart": bool(smart) if not isinstance(smart, str) else smart.casefold() == "true",
            }
        )
    return groups


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Jamf returned HTTP {response.status_code}."
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list):
            parts: list[str] = []
            for item in errors:
                if isinstance(item, dict) and item.get("description"):
                    parts.append(str(item["description"]))
            if parts:
                return "Jamf rejected the change: " + "; ".join(parts)[:300]
        for key in ("message", "error", "description"):
            if payload.get(key):
                return str(payload[key])[:300]
    return f"Jamf returned HTTP {response.status_code}."


def _classic_error_message(response: httpx.Response) -> str:
    """Classic API errors are HTML/XML pages; pull out readable text."""
    import re

    text = response.text or ""
    match = re.search(r"<p>(.*?)</p>", text, re.S | re.I)
    if match:
        text = match.group(1)
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    if not text:
        return f"Jamf returned HTTP {response.status_code}."
    return text[:300]
