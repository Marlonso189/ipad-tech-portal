import json

from tests.conftest import login


def _headers(client):
    return {"X-CSRF-Token": client.get("/api/bootstrap").json()["csrfToken"]}


def test_group_list_is_filtered_and_membership_changes(portal):
    client = portal["client"]
    login(client)
    listed = client.get("/api/groups")
    names = [group["name"] for group in listed.json()["groups"]]
    assert names == ["Tech-Loaner", "Tech-Repair"]

    added = client.post("/api/devices/102/groups", headers=_headers(client), json={"groupIds": ["2"]})
    assert added.status_code == 200, added.text
    assert added.json()["device"]["staticGroups"] == [{"id": "2", "name": "Tech-Repair"}]
    assert "102" in portal["mock"].state.store.membership["2"]

    removed = client.post("/api/devices/102/groups", headers=_headers(client), json={"groupIds": []})
    assert removed.status_code == 200
    assert removed.json()["device"]["staticGroups"] == []
    assert "102" not in portal["mock"].state.store.membership["2"]

    blocked = client.post("/api/devices/101/groups", headers=_headers(client), json={"groupIds": ["4"]})
    assert blocked.status_code == 403
    assert "doesn't have permission" in blocked.json()["message"]
    assert "101" in portal["mock"].state.store.membership["4"]
    assert "101" in portal["mock"].state.store.membership["1"]


def test_empty_group_filter_shows_nothing(portal):
    portal["config"]  # the running app already captured the prefix
    portal["app"].state.portal.config = portal["config"].__class__(
        **{**portal["config"].__dict__, "group_name_prefix": "", "group_allowlist": ()}
    )
    client = portal["client"]
    login(client)
    assert client.get("/api/groups").json()["groups"] == []


def test_allowlist_overrides_prefix(portal):
    portal["app"].state.portal.config = portal["config"].__class__(
        **{**portal["config"].__dict__, "group_allowlist": ("Finance-Locked",), "group_name_prefix": "Tech-"}
    )
    client = portal["client"]
    login(client)
    names = [group["name"] for group in client.get("/api/groups").json()["groups"]]
    assert names == ["Finance-Locked"]


def test_mdm_commands_and_lost_mode(portal):
    client = portal["client"]
    login(client)
    headers = _headers(client)
    restart = client.post("/api/devices/101/commands/restart", headers=headers)
    assert restart.status_code == 200, restart.text
    assert restart.json()["queued"] is True

    inventory = client.post("/api/devices/101/commands/update-inventory", headers=_headers(client))
    assert inventory.status_code == 200
    cleared = client.post("/api/devices/101/commands/clear-passcode", headers=_headers(client))
    assert cleared.status_code == 200

    missing = client.post(
        "/api/devices/101/commands/lost-mode/enable",
        headers=_headers(client),
        json={"message": "", "phone": "", "footnote": ""},
    )
    assert missing.status_code == 400

    enabled = client.post(
        "/api/devices/101/commands/lost-mode/enable",
        headers=_headers(client),
        json={"message": "Please return this iPad", "phone": "555-0100", "footnote": "Front desk"},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["device"]["lostModeEnabled"] is True
    disabled = client.post("/api/devices/101/commands/lost-mode/disable", headers=_headers(client))
    assert disabled.status_code == 200
    assert portal["mock"].state.store.devices["101"].lost_mode is False

    kinds = [item["commandType"] for item in portal["mock"].state.store.commands]
    assert kinds == [
        "RESTART_DEVICE",
        "DEVICE_INFORMATION",
        "CLEAR_PASSCODE",
        "ENABLE_LOST_MODE",
        "DISABLE_LOST_MODE",
    ]


def test_wipe_requires_exact_serial_and_privilege(portal):
    client = portal["client"]
    login(client)
    wrong = client.post(
        "/api/devices/101/commands/erase",
        headers=_headers(client),
        json={"confirmSerial": "dmpx2test001"},
    )
    assert wrong.status_code == 400
    assert "serial" in wrong.json()["message"].lower()
    assert portal["mock"].state.store.commands == []

    erased = client.post(
        "/api/devices/101/commands/erase",
        headers=_headers(client),
        json={"confirmSerial": "DMPX2TEST001"},
    )
    assert erased.status_code == 200, erased.text
    assert portal["mock"].state.store.commands[-1]["commandType"] == "ERASE_DEVICE"

    client.post("/api/logout", headers=_headers(client))
    login(client, "desk", "demo-desk")
    denied = client.post(
        "/api/devices/101/commands/erase",
        headers=_headers(client),
        json={"confirmSerial": "DMPX2TEST001"},
    )
    assert denied.status_code == 403
    assert denied.json()["message"] == "Your Jamf account doesn't have permission for Erase Device."
    assert [item["commandType"] for item in portal["mock"].state.store.commands] == ["ERASE_DEVICE"]
    audit = [
        json.loads(line)
        for line in portal["config"].audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert audit[-1]["action"] == "wipe"
    assert audit[-1]["result"] == "denied"
    assert audit[-1]["username"] == "desk"


def test_jamf_403_is_reported_as_a_permission_failure(portal):
    client = portal["client"]
    login(client)
    portal["mock"].state.store.users["tech"]["privileges"].discard("Send Mobile Device Clear Passcode Command")
    response = client.post("/api/devices/101/commands/clear-passcode", headers=_headers(client))
    assert response.status_code == 403
    assert response.json()["message"] == "Your Jamf account doesn't have permission for Clear Passcode."
    assert portal["mock"].state.store.commands == []


def test_unmanaged_and_unsupervised_devices(portal):
    client = portal["client"]
    login(client)
    unmanaged = client.post("/api/devices/105/commands/restart", headers=_headers(client))
    assert unmanaged.status_code == 409
    portal["mock"].state.store.devices["102"].supervised = False
    restart = client.post("/api/devices/102/commands/restart", headers=_headers(client))
    assert restart.status_code == 409
    assert "not supervised" in restart.json()["message"]
    inventory = client.post("/api/devices/102/commands/update-inventory", headers=_headers(client))
    assert inventory.status_code == 200
