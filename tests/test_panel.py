import copy
import json
from dataclasses import replace

import httpx
import pytest

from vpnbot.panel import Panel, PanelError, StaleClient, fingerprint, update_payload


async def test_full_update_preserves_credentials_and_settings(config, client):
    current = copy.deepcopy(client)
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer secret-panel-token"
        assert request.url.path.startswith("/private/panel/api/clients/")
        if request.method == "POST":
            data = json.loads(request.content)
            assert data == update_payload(client, 300)
            assert data["id"] == client["uuid"]
            for key in ("password", "auth", "flow", "totalGB", "expiryTime", "limitHwid"):
                assert data[key] == client[key]
            current["tgId"] = data["tgId"]
            current["updatedAt"] += 5000
            return httpx.Response(200, json={"success": True})
        return httpx.Response(
            200,
            json={
                "success": True,
                "obj": {
                    "client": {
                        k: v for k, v in current.items() if k not in {"inboundIds", "traffic"}
                    },
                    "inboundIds": [1],
                    "usedTraffic": 4096,
                    "tunnelAllowedIPs": {},
                    "externalLinks": [],
                },
            },
        )

    panel = Panel(config, httpx.MockTransport(handle))
    snapshot = await panel.client("alice")
    await panel.bind("alice", 300, fingerprint(snapshot))
    assert [r.method for r in calls] == ["GET", "GET", "POST", "GET"]
    await panel.close()


async def test_stale_confirmation_never_writes(config, client):
    original = fingerprint(client)
    changed = {**client, "password": "changed-in-panel"}
    methods = []

    def handle(request):
        methods.append(request.method)
        return httpx.Response(200, json={"success": True, "obj": changed})

    panel = Panel(config, httpx.MockTransport(handle))
    with pytest.raises(StaleClient):
        await panel.bind("alice", 300, original)
    assert methods == ["GET"]
    await panel.close()


async def test_write_timeout_is_not_retried_or_exposed(config, client):
    writes = []

    def handle(request):
        if request.method == "POST":
            writes.append(request)
            raise httpx.ReadTimeout("secret in upstream error", request=request)
        return httpx.Response(200, json={"success": True, "obj": client})

    panel = Panel(config, httpx.MockTransport(handle))
    with pytest.raises(PanelError) as error:
        await panel.bind("alice", 300, fingerprint(client))
    assert len(writes) == 1
    assert "secret" not in str(error.value)
    await panel.close()


async def test_session_reauthentication(config, client):
    config = replace(config, panel_token="", panel_username="user", panel_password="pass")
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/login"):
            assert json.loads(request.content) == {"username": "user", "password": "pass"}
            return httpx.Response(200, json={"success": True}, headers={"set-cookie": "session=ok"})
        if len(calls) == 2:
            return httpx.Response(401)
        assert "session=ok" in request.headers["cookie"]
        return httpx.Response(200, json={"success": True, "obj": [client]})

    panel = Panel(config, httpx.MockTransport(handle))
    assert await panel.owned(200) == [client]
    assert calls == ["/private/login", "/private/panel/api/clients/list"] * 2
    await panel.close()


@pytest.mark.parametrize("owner", [0, 300])
async def test_unowned_key_denied(config, client, owner):
    client["tgId"] = owner
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"success": True, "obj": [client]})

    panel = Panel(config, httpx.MockTransport(handle))
    with pytest.raises(PanelError):
        await panel.links(200, "alice")
    assert len(calls) == 1
    await panel.close()


@pytest.mark.parametrize("other_owner,has_subscription", [(200, True), (300, False), (0, False)])
async def test_shared_subscription_does_not_expose_other_clients(
    config,
    client,
    other_owner,
    has_subscription,
):
    other = {**client, "email": "bob", "tgId": other_owner}

    def handle(request):
        path = request.url.path
        if path.endswith("/list"):
            result = [client, other]
        elif "/links/" in path:
            assert path.endswith("/alice")  # never use the all-client subLinks endpoint
            result = ["vless://alice-only@host:443"]
        else:
            result = {"client": client}
        return httpx.Response(200, json={"success": True, "obj": result})

    panel = Panel(config, httpx.MockTransport(handle))
    links, subscription = await panel.links(200, "alice")
    assert links == ["vless://alice-only@host:443"]
    assert bool(subscription) == has_subscription
    await panel.close()


async def test_revocation_during_link_fetch(config, client):
    def handle(request):
        if request.url.path.endswith("/list"):
            obj = [client]
        elif "/links/" in request.url.path:
            obj = ["vless://secret@host:443"]
        else:
            obj = {**client, "tgId": 0}
        return httpx.Response(200, json={"success": True, "obj": obj})

    panel = Panel(config, httpx.MockTransport(handle))
    with pytest.raises(PanelError):
        await panel.links(200, "alice")
    await panel.close()


async def test_api_failure_hides_response_secrets(config):
    panel = Panel(
        config,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"success": False, "msg": "password=supersecret"},
            )
        ),
    )
    with pytest.raises(PanelError) as error:
        await panel.clients()
    assert "supersecret" not in str(error.value)
    await panel.close()


def test_fingerprint_ignores_traffic_but_not_binding(client):
    assert fingerprint(client) == fingerprint({**client, "traffic": {}, "updatedAt": 9999})
    assert fingerprint(client) != fingerprint({**client, "tgId": 300})
