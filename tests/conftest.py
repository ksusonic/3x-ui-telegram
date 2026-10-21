import os
from pathlib import Path

import pytest

from vpnbot.config import Config
from vpnbot.store import Store


@pytest.fixture
def config():
    return Config(
        panel_url="https://panel.example/private/",
        admin_ids=frozenset({100}),
        token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        panel_token="secret-panel-token",
        subscription_url="https://vpn.example/sub/",
        database=Path(":memory:"),
    )


@pytest.fixture
def client():
    return {
        "id": 7,
        "uuid": "a95fb996-37c3-47eb-88c6-d5cd8ecabcaa",
        "email": "alice",
        "subId": "subscription-alice",
        "tgId": 200,
        "enable": True,
        "password": "keep-password",
        "auth": "keep-auth",
        "flow": "xtls-rprx-vision",
        "security": "auto",
        "comment": "Keep me",
        "totalGB": 123456789,
        "expiryTime": 1900000000000,
        "reset": 0,
        "resetDay": 0,
        "resetWeekday": 0,
        "resetMax": 0,
        "trafficReset": "never",
        "trafficResetDay": 1,
        "limitHwid": 3,
        "limitIp": 0,
        "allowedIPs": "",
        "reverse": None,
        "createdAt": 1000,
        "updatedAt": 2000,
        "inboundIds": [1],
        "traffic": {"up": 1024, "down": 3072, "enable": True},
    }


@pytest.fixture(params=["sqlite", "postgres"])
def store(request):
    if request.param == "postgres":
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database")
        from vpnbot.postgres import PostgresStore

        db = PostgresStore(url, require_tls=False)
        assert db.try_lock(), "Test database is in use"
        db.initialize()
        db.db.execute(
            "TRUNCATE chats,actions,jobs,deliveries,audit,cooldown,updates RESTART IDENTITY CASCADE"
        )
    else:
        db = Store(":memory:")
    yield db
    db.close()
