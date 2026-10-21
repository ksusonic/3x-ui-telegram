import json
from urllib.parse import urlencode

import pytest

from vpnbot.catalog import (
    PLATFORMS,
    RECIPES,
    compatibility,
    entry,
    instruction,
    platform_apps,
    snapshot,
)
from vpnbot.formats import Unsupported, export, parse_vless

UUID = "11111111-2222-4333-8444-555555555555"
PUBLIC_KEY = "9OsLZKkN-FAs6cSZHnp8RzqIy8mcwqIYioWMTNx5ZW0"


def link(**options):
    return f"vless://{UUID}@vpn.example:443?" + urlencode(
        {"encryption": "none", "security": "tls", "sni": "front.example", **options}
    )


def test_catalogue_has_complete_unique_platform_snapshot():
    data = snapshot()
    assert data["source"] == "https://docs.rw/clients"
    assert len(data["entries"]) == 92
    assert set(e["platform"] for e in data["entries"]) == set(PLATFORMS)
    assert len({e["entry_id"] for e in data["entries"]}) == 92
    assert set(e["app_id"] for e in data["entries"]) == set(RECIPES)
    for platform in PLATFORMS:
        apps = platform_apps(platform)
        assert apps == sorted(apps, key=lambda a: a["name"].casefold())
        for app in apps:
            assert app["download_url"].startswith("https://")
            assert app["sources"]
            assert len(instruction(app["app_id"], platform)) < 3500
            assert entry(app["app_id"], platform) == app


@pytest.mark.parametrize("family", ["xray", "singbox", "mihomo", "stash"])
@pytest.mark.parametrize("transport", ["tcp", "ws", "grpc"])
def test_exports_preserve_credentials_tls_and_transport(family, transport):
    args = {"type": transport}
    if transport == "ws":
        args.update(path="/socket?a=1", host="ws.example")
    if transport == "grpc":
        args.update(serviceName="vpn-service")
    filename, body = export(link(**args), family)
    document = json.loads(body)
    text = body.decode()
    assert UUID in text and "vpn.example" in text and "front.example" in text
    assert "skip-cert-verify" not in text and "allowInsecure" not in text
    if transport == "ws":
        assert "/socket?a=1" in text and "ws.example" in text
    if transport == "grpc":
        assert "vpn-service" in text
    assert filename.endswith(".yaml" if family in {"mihomo", "stash"} else ".json")
    if family == "stash":
        assert document["proxies"][0]["sni"] == "front.example"
        assert "servername" not in document["proxies"][0]


@pytest.mark.parametrize("family", ["xray", "singbox", "mihomo", "stash"])
def test_reality_and_vision_export(family):
    _, body = export(
        link(security="reality", pbk=PUBLIC_KEY, sid="12ab", flow="xtls-rprx-vision", fp="chrome"),
        family,
    )
    assert PUBLIC_KEY in body.decode() and "12ab" in body.decode()
    assert "xtls-rprx-vision" in body.decode()


@pytest.mark.parametrize(
    "options",
    [
        {"unknown": "secret-value"},
        {"encryption": "unhandled"},
        {"insecure": "1"},
        {"allowInsecure": "true"},
        {"security": "other"},
        {"type": "kcp"},
        {"headerType": "http"},
        {"flow": "other"},
        {"flow": "xtls-rprx-vision", "type": "ws"},
        {"security": "reality", "pbk": "invalid"},
        {"security": "reality", "pbk": PUBLIC_KEY, "sid": "odd"},
    ],
)
def test_unsafe_or_unhandled_options_fail_without_leaking_input(options):
    with pytest.raises(Unsupported) as exc:
        export(link(**options), "xray")
    assert UUID not in str(exc.value)
    assert "secret-value" not in str(exc.value)


@pytest.mark.parametrize(
    "uri",
    [
        "vless://bad@host:443",
        "https://host:443",
        "vless://",
        f"vless://{UUID}@host",
        f"vless://{UUID}:password@host:443",
        f"vless://{UUID}@host:443?type=tcp&type=ws",
    ],
)
def test_malformed_links_fail_without_exposing_credentials(uri):
    with pytest.raises(Unsupported):
        parse_vless(uri)


@pytest.mark.parametrize("family", ["singbox", "mihomo", "stash"])
def test_xhttp_is_not_silently_downgraded(family):
    with pytest.raises(Unsupported):
        export(link(type="xhttp", path="/x"), family)
    document = json.loads(export(link(type="xhttp", path="/x"), "xray")[1])
    assert document["outbounds"][0]["streamSettings"]["xhttpSettings"]["path"] == "/x"


def test_native_app_compatibility_explains_instead_of_sending_wrong_format():
    assert not compatibility("happ", link(type="xhttp"))
    assert compatibility("hiddify", link(type="xhttp"))
    assert not compatibility("loon", link())
    assert not compatibility("anywhere", link())
    assert not compatibility("stash", link())


def test_loon_native_profile_preserves_reality_and_rejects_unknown_transport():
    filename, body = export(
        link(security="reality", pbk=PUBLIC_KEY, sid="12ab", flow="xtls-rprx-vision", fp="chrome"),
        "loon",
    )
    assert filename == "vpn-loon.conf"
    assert b"[Proxy]" in body and b"[Rule]" in body and b"FINAL,VPN" in body
    assert PUBLIC_KEY.encode() in body and UUID.encode() in body
    with pytest.raises(Unsupported):
        export(link(type="grpc"), "loon")


def test_singbox_profile_routes_vpn_and_dns_without_opening_external_listener():
    profile = json.loads(export(link(), "singbox")[1])
    tun = next(i for i in profile["inbounds"] if i["type"] == "tun")
    assert tun["auto_route"] and tun["strict_route"]
    assert profile["dns"]["servers"][1]["detour"] == "proxy"
    assert profile["route"]["default_domain_resolver"] == "bootstrap"


def test_ipv6_and_percent_encoded_path_are_preserved():
    value = link(type="ws", path="/path?test=1&hello=мир").replace("vpn.example", "[2001:db8::1]")
    parsed = parse_vless(value)
    assert parsed.host == "2001:db8::1"
    assert parsed.options["path"] == "/path?test=1&hello=мир"


def test_mlkem_hint_is_honoured_in_mihomo_and_stash():
    uri = link(
        security="reality",
        pbk=PUBLIC_KEY,
        sid="12ab",
        fp="chrome",
        **{"support-x25519mlkem768": "true"},
    )
    for family in ("mihomo", "stash"):
        config = json.loads(export(uri, family)[1])
        assert config["proxies"][0]["reality-opts"]["support-x25519mlkem768"] is True
    assert "realitySettings" in export(uri, "xray")[1].decode()
    with pytest.raises(Unsupported, match="ML-KEM"):
        export(uri, "singbox")
    assert compatibility("hiddify", uri)
    assert not compatibility("happ", uri)


def test_nonportable_reality_spider_settings_are_not_silently_removed():
    uri = link(security="reality", pbk=PUBLIC_KEY, sid="12ab", spx="/spider-path")
    document = json.loads(export(uri, "xray")[1])
    assert (
        document["outbounds"][0]["streamSettings"]["realitySettings"]["spiderX"] == "/spider-path"
    )
    for family in ("singbox", "mihomo", "stash", "loon"):
        with pytest.raises(Unsupported):
            export(uri, family)
