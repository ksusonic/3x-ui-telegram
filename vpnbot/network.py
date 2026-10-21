"""Direct connections only; proxy configuration is intentionally ignored."""

import os

import httpx2
from vercel.queue import QueueClient

PROXY_KEYS = {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "node_use_env_proxy"}


def disable_proxies():
    for key in list(os.environ):
        if key.lower() in PROXY_KEYS:
            os.environ.pop(key, None)


disable_proxies()


def direct_http_client(**kwargs):
    kwargs["trust_env"] = False
    return httpx2.AsyncClient(**kwargs)


async def publish(*args, **kwargs):
    disable_proxies()
    client = QueueClient(http_client_factory=direct_http_client)
    return await client.send(*args, **kwargs)
