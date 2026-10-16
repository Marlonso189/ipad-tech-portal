"""Device actions. Every Jamf call uses the signed-in tech's token."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from app.audit import AuditLog
from app.config import Config
from app.jamf_client import (
    JamfAccessError,
    JamfClient,
    JamfError,
    JamfHTTPError,
    is_ipad,
    parse_device,
)
from app.privileges import FEATURE_LABELS, extract_auth, has_feature, permission_message
from app.sessions import Session

log = logging.getLogger("ipad.auth")

SESSION_EXPIRED = "Your Jamf session is no longer valid. Sign in again."
TIMED_OUT = "Your session timed out. Sign in again."


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Portal:
    def __init__(self, config: Config, jamf: JamfClient, audit: AuditLog) -> None:
        self.config = config
        self.jamf = jamf
        self.audit = audit
        self._cache: dict[tuple[str, str], tuple[float, Any]] = {}

    def authenticate(self, username: str, password: str) -> tuple[str, float, str, str, set[str]]:
        try:
            token, expires = self.jamf.create_token(username, password)
        except JamfAccessError:
            log.info("Login failed")
            raise AppError(401, "credentials", "Those Jamf credentials were not accepted.") from None
        except JamfError as exc:
            log.info("Login failed")
            raise AppError(502, "jamf", exc.message) from exc
        try:
            payload = self.jamf.current_auth(token)
        except JamfError:
            self.jamf.invalidate(token)
            log.info("Login failed")
            raise AppError(
                403,
                "permission",
                "Your Jamf account doesn't have permission for reading account privileges.",
            ) from None
        account_name, privilege_set, privileges = extract_auth(payload)
        log.info("Login succeeded for %s", account_name or username)
        return token, expires, account_name or username, privilege_set, privileges

    def logout(self, session: Session) -> None:
        if session.token:
            self.jamf.invalidate(session.token)
            session.token = None
        log.info("Logout for %s", session.username or "unknown")

    def departments(self, session: Session) -> list[dict[str, str]]:
        self._require(session, "update_location")
        return self._cached(
            session,
            "departments",
            lambda: self._call(session, FEATURE_LABELS["update_location"], self.jamf.list_departments),
        )

    def buildings(self, session: Session) -> list[dict[str, str]]:
        self._require(session, "update_location")
        return self._cached(
            session,
            "buildings",
            lambda: self._call(session, FEATURE_LABELS["update_location"], self.jamf.list_buildings),
        )

    def groups(self, session: Session) -> list[dict[str, str]]:
        self._require(session, "view_groups")
        return self._visible_groups(session)

    def lookup(self, session: Session, query: str) -> dict[str, Any]:
        self._require(session, "lookup")
        text = query.strip()
        if not text or len(text) > 100:
            raise AppError(400, "invalid", "Enter a serial number or asset tag.")
        serial_hits = self._search(session, "serialNumber", text)
        if serial_hits:
            ipads = [device for device in serial_hits if is_ipad(device)]
            if not ipads:
                raise AppError(404, "not_ipad", "That serial number belongs to a mobile device that is not an iPad.")
            if len(ipads) > 1:
                return {"matches": [_summary(device) for device in ipads]}
            return {"device": self._public(self._with_groups(session, ipads[0]))}
        asset_hits = self._search(session, "assetTag", text)
        ipads = [device for device in asset_hits if is_ipad(device)]
        if not ipads:
            if asset_hits:
                raise AppError(404, "not_ipad", "That asset tag belongs to a mobile device that is not an iPad.")
            raise AppError(404, "not_found", "No iPad matched that serial number or asset tag.")
        if len(ipads) > 1:
            return {
                "matches": [_summary(device) for device in ipads],
                "message": "More than one iPad has that asset tag. Choose one.",
            }
        return {"device": self._public(self._with_groups(session, ipads[0]))}

    def get_device(self, session: Session, device_id: str) -> dict[str, Any]:
        self._require(session, "lookup")
        return self._public(self._load(session, device_id))

    def update_location(
        self,
        session: Session,
        device_id: str,
        department_id: str,
        building_id: str,
        room: str,
        update_inventory: bool,
    ) -> dict[str, Any]:
        self._require_change(session, "update_location", device_id)
        device = self._load(session, device_id)
        departments = self.departments(session)
        buildings = self.buildings(session)
        department_id = _known_id(departments, department_id, device["departmentId"], "department")
        building_id = _known_id(buildings, building_id, device["buildingId"], "building")
        room = _bounded(room, 128, "Room")
        before = _location_view(device)
        body = {
            "location": {
                "username": device["username"],
                "realName": device["fullName"],
                "emailAddress": device["email"],
                "departmentId": department_id,
                "buildingId": building_id,
                "room": room,
            }
        }
        return self._apply_change(
            session,
            device,
            action="update_location",
            before=before,
            mutate=lambda token: self.jamf.update_device(token, device["id"], body),
            after_from=lambda updated: _location_view(updated),
            update_inventory=update_inventory,
            action_label=FEATURE_LABELS["update_location"],
        )

    def update_user(
        self,
        session: Session,
        device_id: str,
        username: str,
        full_name: str,
        email: str,
        update_inventory: bool,
    ) -> dict[str, Any]:
        self._require_change(session, "update_user", device_id)
        device = self._load(session, device_id)
        username = _bounded(username, 128, "Username")
        full_name = _bounded(full_name, 128, "Full name")
        email = _bounded(email, 254, "Email")
        if email and ("@" not in email[1:-1]):
            raise AppError(400, "invalid", "Enter a valid email address, or leave it blank.")
        before = _user_view(device)
        body = {
            "location": {
                "username": username,
                "realName": full_name,
                "emailAddress": email,
                "departmentId": device["departmentId"],
                "buildingId": device["buildingId"],
                "room": device["room"],
            }
        }
        return self._apply_change(
            session,
            device,
            action="update_user",
            before=before,
            mutate=lambda token: self.jamf.update_device(token, device["id"], body),
            after_from=_user_view,
            update_inventory=update_inventory,
            action_label=FEATURE_LABELS["update_user"],
        )

    def update_asset_tag(
        self,
        session: Session,
        device_id: str,
        asset_tag: str,
        update_inventory: bool,
    ) -> dict[str, Any]:
        self._require_change(session, "update_asset_tag", device_id)
        device = self._load(session, device_id)
        asset_tag = _bounded(asset_tag, 128, "Asset tag")
        before = {"assetTag": device["assetTag"]}
        return self._apply_change(
            session,
            device,
            action="update_asset_tag",
            before=before,
            mutate=lambda token: self.jamf.update_device(token, device["id"], {"assetTag": asset_tag}),
            after_from=lambda updated: {"assetTag": updated["assetTag"]},
            update_inventory=update_inventory,
            action_label=FEATURE_LABELS["update_asset_tag"],
        )

    def update_groups(self, session: Session, device_id: str, group_ids: list[str]) -> dict[str, Any]:
        self._require_change(session, "update_groups", device_id)
        device = self._load(session, device_id)
        if device.get("groupsError"):
            raise AppError(502, "jamf", "Group membership could not be read, so it was not changed.")
        if device.get("groupsHidden"):
            raise AppError(403, "permission", permission_message("view_groups"))
        visible = {group["id"]: group for group in self._visible_groups(session)}
        desired: set[str] = set()
        for group_id in group_ids:
            if group_id not in visible:
                self._record(
                    session,
                    device,
                    "update_groups",
                    {"groups": [group["name"] for group in device["staticGroups"]]},
                    None,
                    "denied",
                    "A group outside the configured filter was requested.",
                )
                raise AppError(403, "permission", permission_message("update_groups"))
            desired.add(group_id)
        current = {group["id"] for group in device["staticGroups"]}
        before = {"groups": sorted(group["name"] for group in device["staticGroups"])}
        label = FEATURE_LABELS["update_groups"]
        try:
            for group_id in sorted(desired - current):
                self._call(
                    session,
                    label,
                    lambda token, gid=group_id: self.jamf.set_static_membership(token, gid, device["id"], True),
                )
            for group_id in sorted(current - desired):
                self._call(
                    session,
                    label,
                    lambda token, gid=group_id: self.jamf.set_static_membership(token, gid, device["id"], False),
                )
        except AppError as exc:
            self._record(session, device, "update_groups", before, None, _result_for(exc), exc.message)
            raise
        updated = self._load(session, device["id"])
        after = {"groups": sorted(group["name"] for group in updated["staticGroups"])}
        self._record(session, device, "update_groups", before, after, "success", "")
        return {"device": self._public(updated)}

    def restart(self, session: Session, device_id: str) -> dict[str, Any]:
        return self._command(session, device_id, "restart", {"commandType": "RESTART_DEVICE"})

    def update_inventory(self, session: Session, device_id: str) -> dict[str, Any]:
        return self._command(session, device_id, "update_inventory", {"commandType": "DEVICE_INFORMATION"})

    def clear_passcode(self, session: Session, device_id: str) -> dict[str, Any]:
        return self._command(session, device_id, "clear_passcode", {"commandType": "CLEAR_PASSCODE"})

    def enable_lost_mode(self, session: Session, device_id: str, message: str, phone: str, footnote: str) -> dict[str, Any]:
        message = _bounded(message, 256, "Lost Mode message")
        phone = _bounded(phone, 40, "Phone number")
        footnote = _bounded(footnote, 128, "Footnote")
        if not message and not phone:
            raise AppError(400, "invalid", "Lost Mode needs a message, a phone number, or both.")
        command: dict[str, Any] = {"commandType": "ENABLE_LOST_MODE"}
        if message:
            command["lostModeMessage"] = message
        if phone:
            command["lostModePhone"] = phone
        if footnote:
            command["lostModeFootnote"] = footnote
        return self._command(
            session,
            device_id,
            "enable_lost_mode",
            command,
            after={"command": "ENABLE_LOST_MODE", "message": message, "phone": phone, "footnote": footnote},
        )

    def disable_lost_mode(self, session: Session, device_id: str) -> dict[str, Any]:
        return self._command(session, device_id, "disable_lost_mode", {"commandType": "DISABLE_LOST_MODE"})

    def erase(self, session: Session, device_id: str, confirm_serial: str) -> dict[str, Any]:
        return self._command(
            session,
            device_id,
            "wipe",
            {"commandType": "ERASE_DEVICE"},
            confirm_serial=confirm_serial,
        )

    def _command(
        self,
        session: Session,
        device_id: str,
        feature: str,
        command: dict[str, Any],
        *,
        confirm_serial: str | None = None,
        after: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_change(session, feature, device_id)
        try:
            device = self._load(session, device_id)
        except AppError as exc:
            self._record_unknown(session, device_id, feature, exc)
            raise
        before = {"lostModeEnabled": device["lostModeEnabled"], "managed": device["managed"], "supervised": device["supervised"]}
        planned_after = after or {"command": command["commandType"]}
        if confirm_serial is not None and confirm_serial.strip() != device["serialNumber"]:
            exc = AppError(400, "confirm", "Type the device serial exactly to erase this iPad.")
            self._record(session, device, feature, before, None, "error", exc.message)
            raise exc
        if not device["managed"]:
            exc = AppError(409, "unavailable", "This iPad is not managed, so Jamf cannot queue an MDM command.")
            self._record(session, device, feature, before, None, "error", exc.message)
            raise exc
        if not device["managementId"]:
            exc = AppError(409, "unavailable", "This iPad has no Jamf management id, so an MDM command cannot be queued.")
            self._record(session, device, feature, before, None, "error", exc.message)
            raise exc
        if feature in {"restart", "enable_lost_mode", "disable_lost_mode"} and not device["supervised"]:
            exc = AppError(409, "unavailable", "This iPad is not supervised, so Jamf cannot queue that command.")
            self._record(session, device, feature, before, None, "error", exc.message)
            raise exc
        label = FEATURE_LABELS[feature]
        try:
            payload = self._call(
                session,
                label,
                lambda token: self.jamf.send_mdm(token, device["managementId"], command, label),
            )
        except AppError as exc:
            self._record(session, device, feature, before, None, _result_for(exc), exc.message)
            raise
        command_id = str(payload.get("id") or payload.get("commandUuid") or "")
        self._record(session, device, feature, before, planned_after, "success", "")
        try:
            device = self._load(session, device["id"])
        except AppError:
            pass
        return {"queued": True, "commandId": command_id, "device": self._public(device)}

    def _apply_change(
        self,
        session: Session,
        device: dict[str, Any],
        *,
        action: str,
        before: dict[str, Any],
        mutate: Callable[[str], None],
        after_from: Callable[[dict[str, Any]], dict[str, Any]],
        update_inventory: bool,
        action_label: str,
    ) -> dict[str, Any]:
        try:
            self._call(session, action_label, mutate)
        except AppError as exc:
            self._record(session, device, action, before, None, _result_for(exc), exc.message)
            raise
        inventory_status, inventory_detail = self._maybe_inventory(session, device, update_inventory)
        updated = self._load(session, device["id"])
        self._record(session, device, action, before, after_from(updated), "success", inventory_detail)
        response: dict[str, Any] = {"device": self._public(updated), "inventoryUpdate": inventory_status}
        if inventory_status == "queued":
            response["notice"] = inventory_detail
        elif inventory_detail:
            response["warning"] = inventory_detail
        return response

    def _maybe_inventory(self, session: Session, device: dict[str, Any], requested: bool) -> tuple[str, str]:
        if not requested:
            return "not_requested", ""
        if not device["managed"] or not device["managementId"]:
            return "skipped", "Update Inventory was not queued because the iPad is not managed."
        if not has_feature(session.privileges, session.privilege_set, "update_inventory"):
            return "skipped", permission_message("update_inventory")
        label = FEATURE_LABELS["update_inventory"]
        try:
            self._call(
                session,
                label,
                lambda token: self.jamf.send_mdm(
                    token,
                    device["managementId"],
                    {"commandType": "DEVICE_INFORMATION"},
                    label,
                ),
            )
        except AppError as exc:
            return "failed", f"Update Inventory was not queued. {exc.message}"
        return "queued", "Update Inventory was queued."

    def _visible_groups(self, session: Session) -> list[dict[str, str]]:
        groups = self._cached(
            session,
            "groups",
            lambda: self._call(session, FEATURE_LABELS["view_groups"], self.jamf.list_static_groups),
        )
        visible = [group for group in groups if _group_allowed(group["name"], self.config)]
        visible.sort(key=lambda group: group["name"].casefold())
        return visible

    def _with_groups(self, session: Session, device: dict[str, Any]) -> dict[str, Any]:
        if not has_feature(session.privileges, session.privilege_set, "view_groups"):
            device["staticGroups"] = []
            device["otherStaticGroupCount"] = 0
            device["groupsHidden"] = True
            return device
        try:
            visible, other = self._membership(session, device["id"])
        except AppError as exc:
            device["staticGroups"] = []
            device["otherStaticGroupCount"] = 0
            device["groupsError"] = exc.message
            return device
        device["staticGroups"] = visible
        device["otherStaticGroupCount"] = other
        return device

    def _membership(self, session: Session, device_id: str) -> tuple[list[dict[str, str]], int]:
        visible = self._visible_groups(session)
        visible_ids = {group["id"] for group in visible}
        label = FEATURE_LABELS["view_groups"]
        try:
            classic = self._call(session, label, lambda token: self.jamf.device_groups_classic(token, device_id))
        except AppError as exc:
            if exc.status != 404:
                raise
            classic = None
        if classic is None:
            found: list[dict[str, str]] = []
            for group in visible:
                contains = self._call(
                    session,
                    label,
                    lambda token, gid=group["id"]: self.jamf.static_group_contains(token, gid, device_id),
                )
                if contains:
                    found.append({"id": group["id"], "name": group["name"]})
            return found, 0
        shown: list[dict[str, str]] = []
        other = 0
        for group in classic:
            if group.get("smart"):
                continue
            if group["id"] in visible_ids:
                name = next(item["name"] for item in visible if item["id"] == group["id"])
                shown.append({"id": group["id"], "name": name})
            else:
                other += 1
        shown.sort(key=lambda group: group["name"].casefold())
        return shown, other

    def _load(self, session: Session, device_id: str) -> dict[str, Any]:
        _path_safe(device_id)
        hits = self._search(session, "mobileDeviceId", device_id)
        matched = [device for device in hits if device["id"] == device_id]
        if not matched:
            raise AppError(404, "not_found", "Jamf has no mobile device with that id.")
        device = matched[0]
        if not is_ipad(device):
            raise AppError(404, "not_ipad", "That record is not an iPad.")
        return self._with_groups(session, device)

    def _search(self, session: Session, field: str, value: str) -> list[dict[str, Any]]:
        rows = self._call(
            session,
            FEATURE_LABELS["lookup"],
            lambda token: self.jamf.search_devices(token, field, value),
        )
        return [parse_device(row) for row in rows if parse_device(row)["id"]]

    def _public(self, device: dict[str, Any]) -> dict[str, Any]:
        public = dict(device)
        public.pop("managementId", None)
        return public

    def _require(self, session: Session, feature: str) -> None:
        if not has_feature(session.privileges, session.privilege_set, feature):
            raise AppError(403, "permission", permission_message(feature))

    def _require_change(self, session: Session, feature: str, device_id: str) -> None:
        if has_feature(session.privileges, session.privilege_set, feature):
            return
        message = permission_message(feature)
        device = None
        if has_feature(session.privileges, session.privilege_set, "lookup"):
            try:
                device = self._load(session, device_id)
            except AppError:
                device = None
        if device:
            self._record(session, device, feature, None, None, "denied", message)
        else:
            self._record_unknown(session, device_id, feature, AppError(403, "permission", message))
        raise AppError(403, "permission", message)

    def _cached(self, session: Session, kind: str, loader: Callable[[], Any]) -> Any:
        key = (session.username or "", kind)
        cached = self._cache.get(key)
        now = time.time()
        if cached and now - cached[0] < 300:
            return cached[1]
        value = loader()
        self._cache[key] = (now, value)
        return value

    def _call(self, session: Session, action: str, fn: Callable[[str], Any]) -> Any:
        if not session.token:
            raise AppError(401, "session_expired", SESSION_EXPIRED)
        try:
            self._refresh_if_due(session)
        except JamfAccessError:
            raise AppError(401, "session_expired", SESSION_EXPIRED) from None
        except JamfError as exc:
            raise AppError(502, "jamf", exc.message) from exc
        try:
            return fn(session.token)
        except JamfAccessError as err:
            if err.status != 401:
                raise AppError(403, "permission", f"Your Jamf account doesn't have permission for {action}.") from err
            try:
                self._refresh(session)
            except JamfAccessError:
                raise AppError(401, "session_expired", SESSION_EXPIRED) from None
            except JamfError as exc:
                raise AppError(502, "jamf", exc.message) from exc
            try:
                return fn(session.token)
            except JamfAccessError as err2:
                if err2.status == 401:
                    raise AppError(401, "session_expired", SESSION_EXPIRED) from err2
                raise AppError(403, "permission", f"Your Jamf account doesn't have permission for {action}.") from err2
        except JamfHTTPError as err:
            status = 404 if err.status == 404 else 400 if err.status < 500 else 502
            raise AppError(status, "jamf", err.message) from err
        except JamfError as err:
            raise AppError(502, "jamf", err.message) from err

    def _refresh_if_due(self, session: Session) -> None:
        if session.token_expires is None:
            return
        if session.token_expires - time.time() > 300:
            return
        self._refresh(session)

    def _refresh(self, session: Session) -> None:
        if not session.token:
            raise JamfAccessError(401, "refreshing the Jamf session")
        token, expires = self.jamf.keep_alive(session.token)
        session.token = token
        session.token_expires = expires

    def _record(
        self,
        session: Session,
        device: dict[str, Any],
        action: str,
        before: Any,
        after: Any,
        result: str,
        detail: str,
    ) -> None:
        self.audit.write(
            username=session.username or "",
            device_id=str(device.get("id") or ""),
            serial_number=str(device.get("serialNumber") or ""),
            action=action,
            before=before,
            after=after,
            result=result,
            detail=detail,
        )

    def _record_unknown(self, session: Session, device_id: str, action: str, exc: AppError) -> None:
        self.audit.write(
            username=session.username or "",
            device_id=device_id,
            serial_number="",
            action=action,
            before=None,
            after=None,
            result=_result_for(exc),
            detail=exc.message,
        )


def _group_allowed(name: str, config: Config) -> bool:
    if config.group_allowlist:
        allowed = {item.casefold() for item in config.group_allowlist}
        return name.casefold() in allowed
    if config.group_name_prefix:
        return name.casefold().startswith(config.group_name_prefix.casefold())
    return False


def _known_id(items: list[dict[str, str]], selected: str, current: str, label: str) -> str:
    selected = selected.strip()
    if not selected or selected == current:
        return selected
    if not any(item["id"] == selected for item in items):
        raise AppError(400, "invalid", f"Choose a {label} from the list.")
    return selected


def _bounded(value: str, limit: int, label: str) -> str:
    text = value.strip()
    if len(text) > limit:
        raise AppError(400, "invalid", f"{label} is too long.")
    return text


def _path_safe(value: str) -> None:
    if not value or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in value):
        raise AppError(400, "invalid", "That device id is not valid.")


def _summary(device: dict[str, Any]) -> dict[str, str]:
    return {
        "id": device["id"],
        "name": device["name"],
        "serialNumber": device["serialNumber"],
        "assetTag": device["assetTag"],
        "model": device["model"],
    }


def _location_view(device: dict[str, Any]) -> dict[str, str]:
    return {
        "department": device["department"],
        "departmentId": device["departmentId"],
        "building": device["building"],
        "buildingId": device["buildingId"],
        "room": device["room"],
    }


def _user_view(device: dict[str, Any]) -> dict[str, str]:
    return {
        "username": device["username"],
        "fullName": device["fullName"],
        "email": device["email"],
    }


def _result_for(exc: AppError) -> str:
    if exc.status == 403:
        return "denied"
    return "error"
