# SPDX-License-Identifier: AGPL-3.0-or-later
import json

from tests.conftest import login


def _device(client, query):
    response = client.get("/api/devices", params={"q": query})
    assert response.status_code == 200, response.text
    return response.json()["device"]


def test_lookup_by_serial_and_asset_tag(portal):
    client = portal["client"]
    login(client)
    by_serial = _device(client, "DMPX2TEST001")
    assert by_serial["name"] == "Cart iPad 12"
    assert by_serial["model"].startswith("iPad")
    assert by_serial["osVersion"] == "17.6.1"
    assert by_serial["serialNumber"] == "DMPX2TEST001"
    assert by_serial["assetTag"] == "IPAD-1001"
    assert by_serial["username"] == "ada"
    assert by_serial["fullName"] == "Ada Lovelace"
    assert by_serial["email"] == "ada@example.com"
    assert by_serial["department"] == "Science"
    assert by_serial["building"] == "North Hall"
    assert by_serial["room"] == "12"
    assert by_serial["managed"] is True
    assert by_serial["supervised"] is True
    assert by_serial["lastInventory"] == "2026-10-01T15:04:00Z"
    assert by_serial["staticGroups"] == [{"id": "1", "name": "Tech-Loaner"}]
    assert by_serial["otherStaticGroupCount"] == 1
    assert "managementId" not in by_serial

    by_tag = _device(client, "IPAD-1002")
    assert by_tag["serialNumber"] == "DMPX2TEST002"


def test_duplicate_asset_tag_returns_a_choice(portal):
    client = portal["client"]
    login(client)
    response = client.get("/api/devices", params={"q": "IPAD-1001"})
    body = response.json()
    assert response.status_code == 200
    assert "device" not in body
    serials = sorted(item["serialNumber"] for item in body["matches"])
    assert serials == ["DMPX2TEST001", "DMPX2TEST003"]
    chosen = client.get("/api/devices/103")
    assert chosen.json()["device"]["name"] == "Library iPad"
    assert chosen.json()["device"]["staticGroups"] == [{"id": "2", "name": "Tech-Repair"}]


def test_unknown_and_non_ipad(portal):
    client = portal["client"]
    login(client)
    missing = client.get("/api/devices", params={"q": "DOES-NOT-EXIST"})
    assert missing.status_code == 404
    assert "No iPad matched" in missing.json()["message"]
    phone = client.get("/api/devices", params={"q": "DMPXPHONE001"})
    assert phone.status_code == 404
    assert phone.json()["error"] == "not_ipad"


def test_location_user_and_asset_tag_are_audited(portal):
    client = portal["client"]
    login(client)
    headers = {"X-CSRF-Token": client.get("/api/bootstrap").json()["csrfToken"]}
    location = client.patch(
        "/api/devices/102/location",
        headers=headers,
        json={"departmentId": "1", "buildingId": "1", "room": "  Lab A  ", "updateInventory": True},
    )
    assert location.status_code == 200, location.text
    device = location.json()["device"]
    assert device["department"] == "Science"
    assert device["building"] == "North Hall"
    assert device["room"] == "Lab A"
    assert location.json()["inventoryUpdate"] == "queued"
    assert location.json()["notice"] == "Update Inventory was queued."
    assert "warning" not in location.json()

    user = client.patch(
        "/api/devices/102/user",
        headers={"X-CSRF-Token": client.get("/api/bootstrap").json()["csrfToken"]},
        json={"username": "cam", "fullName": "Cam Rivera", "email": "cam@example.com", "updateInventory": False},
    )
    assert user.status_code == 200, user.text
    assert user.json()["device"]["username"] == "cam"
    assert user.json()["device"]["department"] == "Science"
    assert "warning" not in user.json()

    asset = client.patch(
        "/api/devices/102/asset-tag",
        headers={"X-CSRF-Token": client.get("/api/bootstrap").json()["csrfToken"]},
        json={"assetTag": "IPAD-2002", "updateInventory": False},
    )
    assert asset.status_code == 200
    assert asset.json()["device"]["assetTag"] == "IPAD-2002"

    lines = [
        json.loads(line)
        for line in portal["config"].audit_path.read_text(encoding="utf-8").splitlines()
    ]
    inventory_lines = [line for line in lines if line["action"] == "update_inventory"]
    assert len(inventory_lines) == 1
    assert inventory_lines[0]["after"]["path"] == "modern"
    lines = [line for line in lines if line["action"] != "update_inventory"]
    actions = [line["action"] for line in lines]
    assert actions == ["update_location", "update_user", "update_asset_tag"]
    assert lines[0]["before"]["department"] == "Mathematics"
    assert lines[0]["after"]["department"] == "Science"
    assert lines[0]["before"]["room"] == "4"
    assert lines[0]["after"]["room"] == "Lab A"
    assert lines[0]["username"] == "tech"
    assert lines[0]["serialNumber"] == "DMPX2TEST002"
    assert lines[0]["result"] == "success"
    assert lines[1]["before"]["username"] == ""
    assert lines[1]["after"]["email"] == "cam@example.com"
    blob = portal["config"].audit_path.read_text(encoding="utf-8")
    assert "demo-tech" not in blob
    assert "Bearer" not in blob
    inventory = [item for item in portal["mock"].state.store.commands if item["commandType"] == "DEVICE_INFORMATION"]
    assert len(inventory) == 1


def test_invalid_email_and_unknown_department(portal):
    client = portal["client"]
    login(client)
    headers = {"X-CSRF-Token": client.get("/api/bootstrap").json()["csrfToken"]}
    bad_email = client.patch(
        "/api/devices/102/user",
        headers=headers,
        json={"username": "cam", "fullName": "Cam", "email": "not-an-email", "updateInventory": False},
    )
    assert bad_email.status_code == 400
    bad_dept = client.patch(
        "/api/devices/102/location",
        headers=headers,
        json={"departmentId": "999", "buildingId": "1", "room": "4", "updateInventory": False},
    )
    assert bad_dept.status_code == 400
    assert portal["mock"].state.store.devices["102"].department_id == "2"


def test_quoted_asset_tag_round_trip(portal):
    portal["mock"].state.store.devices["102"].asset_tag = 'TAG"1'
    client = portal["client"]
    login(client)
    found = _device(client, 'TAG"1')
    assert found["id"] == "102"


def test_classic_membership_fallback(portal):
    portal["mock"].state.store.classic_enabled = False
    client = portal["client"]
    login(client)
    device = _device(client, "DMPX2TEST001")
    assert device["staticGroups"] == [{"id": "1", "name": "Tech-Loaner"}]
    assert any(path.endswith("/static-group-membership/1") for _method, path in portal["mock"].state.store.requests)
