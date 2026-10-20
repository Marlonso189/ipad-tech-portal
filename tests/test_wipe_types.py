# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wipe: Full wipe (v2 MDM ERASE_DEVICE) vs Return to Service (PreStage-based erase endpoint)."""

import json

from tests.conftest import login


def _headers(client):
    return {"X-CSRF-Token": client.get("/api/bootstrap").json()["csrfToken"]}


def _audit(portal):
    return [json.loads(line) for line in portal["config"].audit_path.read_text(encoding="utf-8").splitlines()]


def test_full_wipe_is_default(portal):
    client = portal["client"]
    login(client)
    response = client.post("/api/devices/101/commands/erase", headers=_headers(client), json={"confirmSerial": "DMPX2TEST001"})
    assert response.status_code == 200, response.text
    store = portal["mock"].state.store
    assert store.commands[-1]["commandType"] == "ERASE_DEVICE"
    assert "returnToService" not in store.commands[-1]
    assert ("POST", "/api/v2/mdm/commands") in store.requests
    entry = _audit(portal)[-1]
    assert entry["action"] == "wipe" and entry["result"] == "success"
    assert entry["after"]["wipeType"] == "full"


def test_return_to_service_uses_prestage_erase_endpoint(portal):
    client = portal["client"]
    login(client)
    response = client.post(
        "/api/devices/101/commands/erase",
        headers=_headers(client),
        json={"confirmSerial": "DMPX2TEST001", "wipeType": "return_to_service"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["queued"] is True
    assert response.json()["commandId"]
    store = portal["mock"].state.store
    assert store.commands[-1]["returnToService"] is True
    assert ("POST", "/api/v2/mobile-devices/101/erase") in store.requests
    assert ("POST", "/api/v2/mdm/commands") not in store.requests
    entry = _audit(portal)[-1]
    assert entry["result"] == "success"
    assert entry["after"]["wipeType"] == "return_to_service"


def test_return_to_service_still_requires_serial(portal):
    client = portal["client"]
    login(client)
    response = client.post(
        "/api/devices/101/commands/erase",
        headers=_headers(client),
        json={"confirmSerial": "wrong", "wipeType": "return_to_service"},
    )
    assert response.status_code == 400
    assert portal["mock"].state.store.commands == []


def test_return_to_service_requirements_not_met(portal):
    client = portal["client"]
    login(client)
    portal["mock"].state.store.return_to_service_ready = False
    response = client.post(
        "/api/devices/101/commands/erase",
        headers=_headers(client),
        json={"confirmSerial": "DMPX2TEST001", "wipeType": "return_to_service"},
    )
    assert response.status_code == 400
    assert "Return to service requirements not met" in response.json()["message"]
    assert portal["mock"].state.store.commands == []
    entry = _audit(portal)[-1]
    assert entry["result"] == "error"
    assert entry["after"]["wipeType"] == "return_to_service"


def test_return_to_service_denied_without_privilege(portal):
    client = portal["client"]
    login(client, "desk", "demo-desk")
    response = client.post(
        "/api/devices/101/commands/erase",
        headers=_headers(client),
        json={"confirmSerial": "DMPX2TEST001", "wipeType": "return_to_service"},
    )
    assert response.status_code == 403
    assert portal["mock"].state.store.commands == []


def test_unknown_wipe_type_rejected(portal):
    client = portal["client"]
    login(client)
    response = client.post(
        "/api/devices/101/commands/erase",
        headers=_headers(client),
        json={"confirmSerial": "DMPX2TEST001", "wipeType": "partial"},
    )
    assert response.status_code in (400, 422)
    assert portal["mock"].state.store.commands == []


def test_wipe_dialog_has_radio_choice_full_default(portal):
    html = portal["client"].get("/").text
    assert 'value="full" checked' in html
    assert 'value="return_to_service"' in html
    assert "Wipe with Return to Service" in html
