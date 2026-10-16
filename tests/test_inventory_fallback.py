"""Update Inventory: Jamf API v2 first, Classic API fallback, both errors surfaced."""

import json

from tests.conftest import login

V2_ERROR = "Unable to perform MDM operation"


def _headers(client):
    return {"X-CSRF-Token": client.get("/api/bootstrap").json()["csrfToken"]}


def _audit(portal):
    return [json.loads(line) for line in portal["config"].audit_path.read_text(encoding="utf-8").splitlines()]


def _classic_path(device_id):
    return f"/JSSResource/mobiledevicecommands/command/UpdateInventory/id/{device_id}"


def test_modern_success_does_not_call_classic(portal):
    client = portal["client"]
    login(client)
    response = client.post("/api/devices/101/commands/update-inventory", headers=_headers(client))
    assert response.status_code == 200, response.text
    store = portal["mock"].state.store
    assert [c.get("api", "v2") for c in store.commands] == ["v2"]
    assert ("POST", _classic_path("101")) not in store.requests
    entry = _audit(portal)[-1]
    assert entry["action"] == "update_inventory"
    assert entry["result"] == "success"
    assert entry["after"]["path"] == "modern"


def test_modern_fails_classic_succeeds(portal):
    client = portal["client"]
    login(client)
    store = portal["mock"].state.store
    store.mdm_failures["DEVICE_INFORMATION"] = (400, V2_ERROR)
    response = client.post("/api/devices/101/commands/update-inventory", headers=_headers(client))
    assert response.status_code == 200, response.text
    assert response.json()["queued"] is True
    assert ("POST", "/api/v2/mdm/commands") in store.requests
    assert ("POST", _classic_path("101")) in store.requests
    assert [c.get("api") for c in store.commands] == ["classic"]
    entry = _audit(portal)[-1]
    assert entry["result"] == "success"
    assert entry["after"]["path"] == "classic"
    assert "Classic API" in entry["detail"]
    assert "Bearer" not in portal["config"].audit_path.read_text(encoding="utf-8")


def test_modern_fails_classic_succeeds_after_save(portal):
    client = portal["client"]
    login(client)
    store = portal["mock"].state.store
    store.mdm_failures["DEVICE_INFORMATION"] = (500, V2_ERROR)
    saved = client.patch(
        "/api/devices/101/asset-tag",
        headers=_headers(client),
        json={"assetTag": "IPAD-9001", "updateInventory": True},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["inventoryUpdate"] == "queued"
    entry = [e for e in _audit(portal) if e["action"] == "update_inventory"][-1]
    assert entry["after"]["path"] == "classic"


def test_both_fail_surfaces_both_errors(portal):
    client = portal["client"]
    login(client)
    store = portal["mock"].state.store
    store.mdm_failures["DEVICE_INFORMATION"] = (400, V2_ERROR)
    store.classic_command_failure = (409, "Unable to queue UpdateInventory command")
    response = client.post("/api/devices/101/commands/update-inventory", headers=_headers(client))
    assert response.status_code == 502
    message = response.json()["message"]
    assert V2_ERROR in message
    assert "Unable to queue UpdateInventory command" in message
    assert store.commands == []
    entry = _audit(portal)[-1]
    assert entry["action"] == "update_inventory"
    assert entry["result"] == "error"
    assert V2_ERROR in entry["detail"] and "UpdateInventory command" in entry["detail"]

    saved = client.patch(
        "/api/devices/101/asset-tag",
        headers=_headers(client),
        json={"assetTag": "IPAD-9002", "updateInventory": True},
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["inventoryUpdate"] == "failed"
    assert V2_ERROR in body["warning"] and "UpdateInventory command" in body["warning"]
