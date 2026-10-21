import runpy
import sqlite3
from pathlib import Path

import pytest

from vpnbot.config import Config
from vpnbot.panel import update_payload


@pytest.mark.parametrize(
    "url",
    [
        "http://panel.example/",
        "https://user:password@panel.example/",
        "https://panel.example/?token=secret",
        "https://panel.example/#fragment",
        "",
    ],
)
def test_panel_rejects_insecure_or_embedded_credentials(monkeypatch, url):
    monkeypatch.setenv("PANEL_BASE_URL", url)
    monkeypatch.setenv("PANEL_API_TOKEN", "secret")
    with pytest.raises(ValueError):
        Config.from_env(panel_only=True)


def test_config_hides_secrets_in_repr_and_allows_panel_only(monkeypatch):
    for name in ("BOT_TOKEN", "ADMIN_IDS", "PANEL_USERNAME", "PANEL_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PANEL_BASE_URL", "https://panel.example/private")
    monkeypatch.setenv("PANEL_API_TOKEN", "supersecret")
    config = Config.from_env(panel_only=True)
    assert config.panel_url == "https://panel.example/private/"
    assert "supersecret" not in repr(config)
    with pytest.raises(ValueError):
        Config.from_env()


def test_tunnel_fields_and_reverse_survive_update(client):
    reverse = {"tag": "reverse", "domains": ["example.com"]}
    client.update(
        {
            "allowedIPs": "10.0.0.2/32, fd00::2/128",
            "reverse": reverse,
            "tunnelAllowedIPs": {"1": "10.0.0.2/32", "2": "10.1.0.2/32,fd01::2/128"},
            "keepAlive": 25,
            "forwardedPorts": "80,443",
        }
    )
    result = update_payload(client, 300)
    assert result["allowedIPs"] == ["10.0.0.2/32", "fd00::2/128"]
    assert result["allowedIPsByInbound"] == {
        "1": ["10.0.0.2/32"],
        "2": ["10.1.0.2/32", "fd01::2/128"],
    }
    assert result["reverse"] == reverse
    assert result["keepAlive"] == 25
    assert result["forwardedPorts"] == "80,443"
    assert result["created_at"] == client["createdAt"]
    assert "createdAt" not in result


def test_backup_includes_committed_wal_and_has_private_permissions(tmp_path):
    backup = runpy.run_path(str(Path(__file__).parents[1] / "deploy" / "backup.py"))["backup"]
    source, target = tmp_path / "source.sqlite3", tmp_path / "backup.sqlite3"
    live = sqlite3.connect(source)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE jobs (body TEXT)")
    live.execute("INSERT INTO jobs VALUES ('notification')")
    live.commit()
    backup(source, target)
    restored = sqlite3.connect(target)
    assert restored.execute("SELECT body FROM jobs").fetchone()[0] == "notification"
    assert target.stat().st_mode & 0o777 == 0o600
    restored.close()
    live.close()


def test_backup_never_creates_missing_source(tmp_path):
    backup = runpy.run_path(str(Path(__file__).parents[1] / "deploy" / "backup.py"))["backup"]
    source, target = tmp_path / "missing.sqlite3", tmp_path / "backup.sqlite3"
    with pytest.raises(ValueError):
        backup(source, target)
    assert not source.exists() and not target.exists()
