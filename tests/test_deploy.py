"""Deployment files stay generic and keep the portal as a single process."""

import json
from pathlib import Path, PurePosixPath

import yaml

from app.config import load_config


def _posix_path(path) -> PurePosixPath:
    """Compare container paths without a Windows drive letter."""
    text = path.as_posix()
    if len(text) >= 2 and text[1] == ":":
        text = text[2:]
    return PurePosixPath(text)


def test_deployment_examples_have_no_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("ITP_JAMF_URL", raising=False)
    names = [
        "config.example.yaml",
        "config.docker.example.yaml",
        "desktop/config.example.yaml",
        ".env.example",
    ]
    for name in names:
        text = Path(name).read_text(encoding="utf-8")
        parsed = yaml.safe_load(text) if name.endswith((".yaml", ".yml")) else {}
        blob = json.dumps(parsed).lower() if parsed else text.lower()
        assert "password" not in blob
        assert "secret" not in blob or "no credentials" in text.lower() or "no secrets" in text.lower()

    destination = tmp_path / "config.yaml"
    destination.write_text(Path("config.docker.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    config = load_config(destination)
    assert config.host == "0.0.0.0"
    assert config.cookie_secure is True
    assert _posix_path(config.audit_path) == PurePosixPath("/app/logs/audit.jsonl")
    assert _posix_path(config.log_path) == PurePosixPath("/app/logs/app.log")
    assert config.jamf_url == "https://jamf.example.org:8443"


def test_compose_publishes_https_only_and_mock_override_points_at_the_mock():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    mock = Path("docker-compose.mock.yml").read_text(encoding="utf-8")
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    internal = Path("deploy/Caddyfile.internal").read_text(encoding="utf-8")
    provided = Path("deploy/Caddyfile").read_text(encoding="utf-8")

    assert "unless-stopped" in compose
    assert "8741:8741" not in compose
    assert "portal-logs" in compose
    assert ":ro" in compose
    assert "password" not in compose.lower()
    assert "ITP_JAMF_URL" in compose

    assert "http://mock:8742" in mock
    assert "MOCK_JAMF_HOST" in mock
    assert "8742:8742" not in mock

    assert "USER portal" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "requirements-desktop" not in dockerfile

    assert "tls internal" in internal
    assert "reverse_proxy portal:8741" in internal
    assert "/certs/cert.pem" in provided
    assert "/certs/key.pem" in provided
    assert "reverse_proxy portal:8741" in provided

    installer = Path("desktop/installer.iss").read_text(encoding="utf-8")
    assert "iPad Tech Portal" in installer
    assert "{commonappdata}\\ipad-tech-portal" in installer
    assert "users-readexec" in installer
    workflow = Path(".github/workflows/desktop.yml").read_text(encoding="utf-8")
    assert "windows-latest" in workflow
    assert "build.ps1" in workflow


def test_paste_in_compose_pulls_ghcr_and_does_not_build():
    deploy = Path("deploy/docker-compose.yml").read_text(encoding="utf-8")
    publish = Path(".github/workflows/publish-image.yml").read_text(encoding="utf-8")
    script = Path("scripts/publish-image.sh").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "ghcr.io/marlonso189/ipad-tech-portal:latest" in deploy
    assert "image: caddy:2.10" in deploy
    assert "\n    build:" not in deploy
    assert "unless-stopped" in deploy
    assert "portal-logs:" in deploy
    assert "caddy-data:" in deploy
    assert "replicas: 1" in deploy
    assert "ITP_HOSTNAME" in deploy
    assert "tls internal" in deploy
    assert "ITP_TLS_CERT" in deploy
    assert "pull_policy: always" in deploy
    assert "password" not in deploy.lower()

    assert "packages: write" in publish
    assert "linux/amd64,linux/arm64" in publish
    assert "refs/heads/main" in publish
    assert ":latest" in publish
    assert "GITHUB_SHA" in publish
    assert "refs/tags/v" in publish
    assert "github.com/Marlonso189/ipad-tech-portal" in publish
    assert "Cursor Origin" in publish

    assert "GHCR_TOKEN" in script
    assert "GHCR_USERNAME" in script
    assert "docker buildx build" in script
    assert "--platform linux/amd64,linux/arm64" in script
    assert "ghp_" not in script
    assert "github_pat_" not in script

    fence = "```yaml\n# Paste this file into Dockhand"
    assert fence in readme
    pasted = readme[readme.index(fence) + len("```yaml\n") :].split("\n```", 1)[0]
    assert pasted + "\n" == deploy
    assert "read:packages" in readme
    assert "Marlonso189" in readme
    assert "Deploy with Dockhand" in readme
