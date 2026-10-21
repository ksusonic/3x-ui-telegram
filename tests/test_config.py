import pytest

from vpnbot.config import Config
from vpnbot.panel import update_payload


@pytest.mark.parametrize("value", ["0", "123", "-987654321,123"])
def test_admin_group_ids_must_be_negative(monkeypatch, value):
    monkeypatch.setenv("ADMIN_CHAT_IDS", value)
    with pytest.raises(ValueError, match="ADMIN_CHAT_IDS"):
        Config.from_env(panel_only=True)


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


def test_panel_credentials_cannot_replace_api_token(monkeypatch):
    monkeypatch.setenv("PANEL_BASE_URL", "https://panel.example/private")
    monkeypatch.delenv("PANEL_API_TOKEN", raising=False)
    monkeypatch.setenv("PANEL_USERNAME", "old-admin")
    monkeypatch.setenv("PANEL_PASSWORD", "old-password")
    with pytest.raises(ValueError, match="PANEL_API_TOKEN"):
        Config.from_env(panel_only=True)


def test_proxy_environment_is_disabled(monkeypatch):
    from vpnbot.network import disable_proxies

    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    for key in keys:
        monkeypatch.setenv(key, "http://127.0.0.1:9999")
    disable_proxies()
    import os

    assert all(key not in os.environ for key in keys)


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
