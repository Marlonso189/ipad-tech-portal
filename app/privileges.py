"""Map portal features to Jamf Pro privilege names.

The strings are the privilege names returned by GET /api/v1/auth
(account.privilegesBySite). A few older Jamf account-UI labels are accepted
as aliases so a custom privilege set still unlocks the matching button.
"""

from __future__ import annotations

# Each feature needs every group, and a group is satisfied by any one name.
FEATURE_REQUIREMENTS: dict[str, tuple[tuple[str, ...], ...]] = {
    "lookup": (("Read Mobile Devices",),),
    "update_location": (
        ("Update Mobile Devices",),
        ("Read Departments",),
        ("Read Buildings",),
    ),
    "update_user": (("Update Mobile Devices",),),
    "update_asset_tag": (("Update Mobile Devices",),),
    "view_groups": (
        ("Read Static Mobile Device Groups",),
        ("Read Mobile Devices",),
    ),
    "update_groups": (("Update Static Mobile Device Groups",),),
    "restart": (
        ("View MDM command information in Jamf Pro API",),
        ("Send Mobile Device Restart Device Command",),
    ),
    "update_inventory": (
        ("View MDM command information in Jamf Pro API",),
        (
            "Send Mobile Device Update Inventory Command",
            "Send Inventory Requests to Mobile Devices",
        ),
    ),
    "clear_passcode": (
        ("View MDM command information in Jamf Pro API",),
        (
            "Send Mobile Device Clear Passcode Command",
            "Send Mobile Device Remove Passcode Command",
        ),
    ),
    "enable_lost_mode": (
        ("View MDM command information in Jamf Pro API",),
        (
            "Send Mobile Device Enable Lost Mode Command",
            "Send Mobile Device Lost Mode Command",
        ),
    ),
    "disable_lost_mode": (
        ("View MDM command information in Jamf Pro API",),
        (
            "Send Mobile Device Disable Lost Mode Command",
            "Send Mobile Device Lost Mode Command",
        ),
    ),
    "wipe": (
        ("View MDM command information in Jamf Pro API",),
        ("Send Mobile Device Remote Wipe Command",),
    ),
}

FEATURE_LABELS: dict[str, str] = {
    "lookup": "looking up iPads",
    "update_location": "changing department, building, or room",
    "update_user": "changing the assigned user",
    "update_asset_tag": "changing the asset tag",
    "view_groups": "viewing static groups",
    "update_groups": "changing static group membership",
    "restart": "Restart Device",
    "update_inventory": "Update Inventory",
    "clear_passcode": "Clear Passcode",
    "enable_lost_mode": "enabling Lost Mode",
    "disable_lost_mode": "disabling Lost Mode",
    "wipe": "Erase Device",
}

# Privileges a full tech account needs. The mock and the README share this list.
TECH_PRIVILEGES: tuple[str, ...] = (
    "Read Mobile Devices",
    "Update Mobile Devices",
    "Read Departments",
    "Read Buildings",
    "Read Static Mobile Device Groups",
    "Update Static Mobile Device Groups",
    "View MDM command information in Jamf Pro API",
    "Send Mobile Device Restart Device Command",
    "Send Mobile Device Update Inventory Command",
    "Send Mobile Device Clear Passcode Command",
    "Send Mobile Device Enable Lost Mode Command",
    "Send Mobile Device Disable Lost Mode Command",
    "Send Mobile Device Remote Wipe Command",
)


def permission_message(feature: str) -> str:
    label = FEATURE_LABELS.get(feature, feature)
    return f"Your Jamf account doesn't have permission for {label}."


def has_feature(privileges: set[str], privilege_set: str, feature: str) -> bool:
    if privilege_set.upper() == "ADMINISTRATOR":
        return True
    requirements = FEATURE_REQUIREMENTS.get(feature)
    if requirements is None:
        return False
    for choices in requirements:
        if not any(name in privileges for name in choices):
            return False
    return True


def feature_map(privileges: set[str], privilege_set: str) -> dict[str, bool]:
    return {
        feature: has_feature(privileges, privilege_set, feature)
        for feature in FEATURE_REQUIREMENTS
    }


def extract_auth(payload: dict) -> tuple[str, str, set[str]]:
    """Return username, privilege set, and the union of site privileges."""
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    username = str(account.get("username") or "")
    privilege_set = str(account.get("privilegeSet") or "")
    found: set[str] = set()
    by_site = account.get("privilegesBySite")
    if isinstance(by_site, dict):
        for names in by_site.values():
            if isinstance(names, list):
                found.update(str(name) for name in names)
    for container in (account, payload):
        flat = container.get("privileges")
        if isinstance(flat, list):
            found.update(str(name) for name in flat)
    return username, privilege_set, found
