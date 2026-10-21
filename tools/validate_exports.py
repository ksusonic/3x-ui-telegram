"""Validate synthetic profiles using separately installed official engine executables."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from vpnbot.formats import export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for family in ("xray", "singbox", "mihomo"):
        parser.add_argument("--" + family, required=True, type=Path)
    args = parser.parse_args()
    env = {
        k: v
        for k, v in os.environ.items()
        if k.lower()
        not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "node_use_env_proxy"}
    }
    cases = [
        {"type": "tcp"},
        {
            "type": "tcp",
            "security": "reality",
            "pbk": "9OsLZKkN-FAs6cSZHnp8RzqIy8mcwqIYioWMTNx5ZW0",
            "sid": "12ab",
            "flow": "xtls-rprx-vision",
            "fp": "chrome",
        },
        {"type": "ws", "path": "/websocket", "host": "front.example"},
        {"type": "grpc", "serviceName": "vpn-service"},
        {"type": "tcp", "security": "none", "sni": ""},
        {"type": "httpupgrade", "path": "/upgrade", "host": "front.example"},
        {"type": "xhttp", "path": "/xhttp", "mode": "auto"},
        {
            "type": "tcp",
            "security": "reality",
            "pbk": "9OsLZKkN-FAs6cSZHnp8RzqIy8mcwqIYioWMTNx5ZW0",
            "sid": "12ab",
            "flow": "xtls-rprx-vision",
            "fp": "chrome",
            "support-x25519mlkem768": "true",
        },
    ]
    passed = 0
    with tempfile.TemporaryDirectory(prefix="vpn-profile-check-") as tmp:
        for family in ("xray", "singbox", "mihomo"):
            for i, options in enumerate(cases):
                if options["type"] == "xhttp" and family != "xray":
                    continue
                if options["type"] == "httpupgrade" and family == "mihomo":
                    continue
                if options.get("support-x25519mlkem768") and family == "singbox":
                    continue
                params = {
                    "encryption": "none",
                    "security": "tls",
                    "sni": "front.example",
                    **options,
                }
                uri = "vless://11111111-2222-4333-8444-555555555555@127.0.0.1:443?" + urlencode(
                    params
                )
                filename, content = export(uri, family)
                path = Path(tmp) / f"{i}-{filename}"
                path.write_bytes(content)
                command = {
                    "xray": [str(args.xray), "run", "-test", "-config", str(path)],
                    "singbox": [str(args.singbox), "check", "-c", str(path)],
                    "mihomo": [str(args.mihomo), "-t", "-d", tmp, "-f", str(path)],
                }[family]
                result = subprocess.run(
                    command, capture_output=True, text=True, env=env, timeout=20
                )
                if result.returncode:
                    # Only synthetic configurations are used by this tool.
                    print(f"FAIL {family} case={i}: {result.stdout}{result.stderr}")
                    raise SystemExit(1)
                passed += 1
                print(f"PASS {family} {options['type']} {params['security']}")
    print(json.dumps({"engine_checks_passed": passed}))


if __name__ == "__main__":
    main()
