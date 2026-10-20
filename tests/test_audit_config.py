import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

import yaml

from app.audit import AUDIT_FALLBACK_WARNING, AuditLog
from app.config import ConfigError, load_config
from app.locking import exclusive_lock
from app.jamf_client import _classic_groups, _rsql_quote
from app.privileges import has_feature
from app.redact import redact


def test_classic_group_shapes_and_rsql_quoting():
    single = {
        "mobile_device": {
            "mobile_device_groups": {
                "mobile_device_group": {"id": 2, "name": "Tech-Repair", "is_smart": False}
            }
        }
    }
    assert _classic_groups(single) == [{"id": "2", "name": "Tech-Repair", "smart": False}]
    assert _rsql_quote('TAG"1') == '"TAG\\"1"'


def test_redact_removes_secrets():
    cleaned = redact({
        "username": "tech",
        "password": "s3cret",
        "token": "abc",
        "nested": {"Authorization": "Bearer abc.def"},
        "note": "Authorization: Bearer abc.def",
    })
    assert cleaned["username"] == "tech"
    assert cleaned["password"] == "[redacted]"
    assert cleaned["token"] == "[redacted]"
    assert cleaned["nested"]["Authorization"] == "[redacted]"
    assert "abc.def" not in cleaned["note"]


def test_administrator_privilege_set_unlocks_wipe():
    assert has_feature(set(), "ADMINISTRATOR", "wipe") is True
    assert has_feature({"Read Mobile Devices"}, "CUSTOM", "wipe") is False


def test_audit_rotates_and_purges(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path, retention_days=90, max_bytes=180)
    for index in range(6):
        audit.write(
            username="tech",
            device_id="101",
            serial_number="DMPX2TEST001",
            action="update_asset_tag",
            before={"assetTag": str(index)},
            after={"assetTag": str(index + 1)},
            result="success",
        )
    rotated = list(tmp_path.glob("audit-*.jsonl"))
    assert rotated
    assert path.exists()
    stale = tmp_path / "audit-20000101T000000000000Z.jsonl"
    stale.write_text('{"stale":true}\n', encoding="utf-8")
    old = time.time() - (120 * 86400)
    import os
    os.utime(stale, (old, old))
    audit.write(
        username="tech",
        device_id="101",
        serial_number="DMPX2TEST001",
        action="restart",
        before=None,
        after={"command": "RESTART_DEVICE"},
        result="success",
    )
    assert not stale.exists()
    recent = next(tmp_path.glob("audit-2*.jsonl"))
    text = recent.read_text(encoding="utf-8") + path.read_text(encoding="utf-8")
    assert '"password"' not in text
    json.loads(path.read_text(encoding="utf-8").splitlines()[-1])


def _audit_child(path: str, ident: str, count: int) -> None:
    audit = AuditLog(Path(path), retention_days=90, max_bytes=10_000_000)
    for index in range(count):
        audit.write(
            username="tech",
            device_id="101",
            serial_number="DMPX2TEST001",
            action="update_asset_tag",
            before={"assetTag": ident},
            after={"assetTag": f"{ident}-{index}"},
            result="success",
            detail=f"{ident}-{index}",
        )


@pytest.mark.skipif(os.name == "nt", reason="Windows locks exclude other processes; the spawn test covers that.")
def test_audit_lock_blocks_a_second_writer(tmp_path: Path):
    order: list[str] = []
    lock_path = tmp_path / "audit.jsonl.lock"

    def hold() -> None:
        with exclusive_lock(lock_path):
            order.append("in")
            time.sleep(0.3)
            order.append("out")

    worker = threading.Thread(target=hold)
    worker.start()
    time.sleep(0.05)
    with exclusive_lock(lock_path):
        order.append("second")
    worker.join()
    assert order == ["in", "out", "second"]


def test_concurrent_processes_append_whole_lines(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_audit_child, args=(str(path), ident, 25))
        for ident in ("a", "b")
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 50
    details = [json.loads(line)["detail"] for line in lines]
    assert len(set(details)) == 50
    assert (tmp_path / "audit.jsonl.lock").exists()
    assert not list(tmp_path.glob("audit.jsonl.lock-*"))


def test_audit_write_falls_back_when_the_share_rejects_appends(tmp_path: Path):
    primary = tmp_path / "share" / "audit.jsonl"
    fallback = tmp_path / "local" / "audit.jsonl"
    audit = AuditLog(primary, retention_days=90, max_bytes=10_000_000, fallback_path=fallback)
    assert audit.warning == ""
    primary.chmod(0o444)
    try:
        audit.write(
            username="tech",
            device_id="101",
            serial_number="DMPX2TEST001",
            action="restart",
            before=None,
            after={"command": "RESTART_DEVICE"},
            result="success",
        )
    finally:
        primary.chmod(0o644)
    assert audit.path == fallback
    assert audit.warning == AUDIT_FALLBACK_WARNING
    saved = json.loads(fallback.read_text(encoding="utf-8").strip())
    assert saved["action"] == "restart"
    assert "password" not in fallback.read_text(encoding="utf-8")


def test_example_config_loads(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ITP_JAMF_URL", raising=False)
    example = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    assert "password" not in json.dumps(example).lower()
    assert example["insecure_test_mode"] is False
    assert "url" not in example.get("jamf", {})
    destination = tmp_path / "config.yaml"
    destination.write_text(Path("config.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    config = load_config(destination)
    assert config.port == 8741
    assert config.idle_timeout_seconds == 15 * 60
    assert config.group_name_prefix == "Tech-"
    assert config.audit_retention_days == 90
    assert config.verify_tls is True
    assert config.insecure_test_mode is False
    assert config.cookie_secure is True
    assert config.audit_path.parent == (tmp_path / "logs").resolve()
    assert config.audit_fallback_path is None


def test_missing_config(tmp_path: Path):
    try:
        load_config(tmp_path / "missing.yaml")
    except ConfigError as exc:
        assert "config.example.yaml" in str(exc)
    else:
        raise AssertionError("expected ConfigError")
