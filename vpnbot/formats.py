"""Local, conservative VLESS exporters. Never drop unrecognised URI parameters."""

import base64
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlsplit
from uuid import UUID


class Unsupported(ValueError):
    """A safe explanation, without credentials or the original URI."""


@dataclass(frozen=True, repr=False)
class Vless:
    host: str
    port: int
    uuid: str
    options: dict[str, str]


def parse_vless(link: str) -> Vless:
    try:
        if any(ord(c) < 32 for c in link):
            raise ValueError
        url = urlsplit(link)
        if url.scheme != "vless" or not url.hostname or not url.username or url.password:
            raise ValueError
        credential = str(UUID(unquote(url.username)))
        port = url.port
        if not port or not 0 < port < 65536 or url.path not in {"", "/"}:
            raise ValueError
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
        options = dict(pairs)
        if len(options) != len(pairs):
            raise ValueError
        if any(ord(c) < 32 for value in options.values() for c in value):
            raise ValueError
        if any(ord(c) < 32 for c in url.hostname):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise Unsupported("Нужна корректная ссылка VLESS с UUID и портом") from None
    known = {
        "encryption",
        "flow",
        "type",
        "security",
        "sni",
        "fp",
        "pbk",
        "sid",
        "spx",
        "alpn",
        "host",
        "path",
        "serviceName",
        "mode",
        "authority",
        "headerType",
        "extra",
        "allowInsecure",
        "insecure",
        "ech",
        "packetEncoding",
        "seed",
        "support-x25519mlkem768",
    }
    if options.keys() - known:
        raise Unsupported("В ссылке есть дополнительные параметры; используйте исходную ссылку")
    if options.get("encryption", "none") != "none":
        raise Unsupported("Экспорт VLESS Encryption пока недоступен; используйте Xray-приложение")
    if any(
        options.get(k, "").lower() not in {"", "0", "false"} for k in ("allowInsecure", "insecure")
    ):
        raise Unsupported("Конфигурации с отключённой проверкой TLS не экспортируются")
    if options.get("security", "none") not in {"none", "tls", "reality"}:
        raise Unsupported("Этот режим защиты не поддерживается экспортом")
    if options.get("support-x25519mlkem768", "") not in {"", "0", "1", "true", "false"}:
        raise Unsupported("Некорректный флаг поддержки ML-KEM")
    if options.get("support-x25519mlkem768") and options.get("security") != "reality":
        raise Unsupported("Флаг ML-KEM применим только к Reality")
    if options.get("flow", "") not in {"", "xtls-rprx-vision"}:
        raise Unsupported("Этот режим VLESS flow не поддерживается экспортом")
    transport = options.get("type", "tcp")
    if transport not in {"tcp", "raw", "ws", "grpc", "httpupgrade", "xhttp"}:
        raise Unsupported("Этот транспорт не поддерживается экспортом")
    if options.get("headerType", "none") not in {"none", ""}:
        raise Unsupported("Маскировка TCP-заголовков не поддерживается экспортом")
    if options.get("flow") and (
        transport not in {"tcp", "raw"} or options.get("security") not in {"tls", "reality"}
    ):
        raise Unsupported("VLESS Vision требует TCP и TLS или Reality")
    if options.get("security") == "reality":
        try:
            key = options["pbk"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", key):
                raise ValueError
            if len(base64.urlsafe_b64decode(key + "=")) != 32:
                raise ValueError
            if not re.fullmatch(r"(?:[a-fA-F0-9]{2}){0,8}", options.get("sid", "")):
                raise ValueError
            if not options.get("sni"):
                raise ValueError
        except (KeyError, ValueError):
            raise Unsupported("Для Reality нужны корректные public key, short ID и SNI") from None
    if options.get("fp") and options["fp"] not in {
        "chrome",
        "firefox",
        "safari",
        "ios",
        "android",
        "edge",
        "360",
        "qq",
        "random",
        "randomized",
    }:
        raise Unsupported("Этот TLS fingerprint не поддерживается экспортом")
    return Vless(url.hostname, port, credential, options)


def export(link: str, family: str) -> tuple[str, bytes]:
    value = parse_vless(link)
    q = value.options
    transport = q.get("type", "tcp")
    secure = q.get("security", "none")
    # These fields must not be silently lost by an exporter.
    if q.get("ech") or q.get("seed") or q.get("packetEncoding"):
        raise Unsupported("ECH, seed и packetEncoding требуют импорта исходной ссылки")
    if transport != "xhttp" and any(q.get(k) for k in ("extra", "mode")):
        raise Unsupported("Дополнительные настройки транспорта требуют исходной ссылки")
    if transport != "grpc" and (q.get("serviceName") or q.get("authority")):
        raise Unsupported("Дополнительные настройки gRPC требуют исходной ссылки")
    if transport in {"tcp", "raw"} and (q.get("path") or q.get("host")):
        raise Unsupported("Дополнительные настройки TCP требуют исходной ссылки")
    if secure == "none" and any(q.get(k) for k in ("sni", "fp", "alpn", "pbk", "sid", "spx")):
        raise Unsupported("В ссылке смешаны параметры защищённого и обычного соединения")
    if secure != "reality" and any(q.get(k) for k in ("pbk", "sid", "spx")):
        raise Unsupported("Параметры Reality требуют режима Reality")
    if family == "xray":
        result = _xray(value)
    elif family == "singbox":
        result = _singbox(value)
    elif family == "mihomo":
        result = _mihomo(value)
    elif family == "stash":
        result = _stash(value)
    elif family == "loon":
        return "vpn-loon.conf", _loon(value).encode()
    else:
        raise Unsupported("Для приложения не выбран проверенный формат конфигурации")
    extension = "yaml" if family in {"mihomo", "stash"} else "json"
    # JSON is a YAML 1.2 subset; quoting is unambiguous, including unusual server names.
    return f"vpn-{family}.{extension}", json.dumps(result, ensure_ascii=False, indent=2).encode()


def _xray(v: Vless):
    q = v.options
    # support-x25519mlkem768 is an import hint for third-party clients. Native Xray
    # automatically negotiates ML-KEM; it has no equivalent JSON toggle.
    network = q.get("type", "tcp")
    network = "raw" if network == "tcp" else network
    stream = {"network": network, "security": q.get("security", "none")}
    if network in {"ws", "httpupgrade"}:
        stream[network + "Settings"] = {"path": q.get("path", "/")}
        if q.get("host"):
            if network == "ws":
                stream[network + "Settings"]["headers"] = {"Host": q["host"]}
            else:
                stream[network + "Settings"]["host"] = q["host"]
    if network == "grpc":
        if q.get("host") or q.get("path"):
            raise Unsupported("Неоднозначные параметры gRPC; используйте исходную ссылку")
        stream["grpcSettings"] = {"serviceName": q.get("serviceName", "")}
        if q.get("authority"):
            stream["grpcSettings"]["authority"] = q["authority"]
    if network == "xhttp":
        settings = {"path": q.get("path", "/"), "mode": q.get("mode", "auto")}
        if settings["mode"] not in {"auto", "packet-up", "stream-up", "stream-one"}:
            raise Unsupported("Неизвестный режим XHTTP")
        if q.get("host"):
            settings["host"] = q["host"]
        if q.get("extra"):
            try:
                extra = json.loads(q["extra"])
                if not isinstance(extra, dict):
                    raise ValueError
                # Client-supplied nested diallers can change the destination of a profile.
                allowed = {
                    "noGRPCHeader",
                    "noSSEHeader",
                    "scMaxEachPostBytes",
                    "scMinPostsIntervalMs",
                    "xPaddingBytes",
                    "scMaxBufferedPosts",
                }
                if extra.keys() - allowed:
                    raise ValueError
                settings["extra"] = extra
            except ValueError:
                raise Unsupported(
                    "Дополнительные настройки XHTTP требуют исходной ссылки"
                ) from None
        stream["xhttpSettings"] = settings
    security = stream["security"]
    if security != "none":
        tls = {"serverName": q.get("sni", v.host), "fingerprint": q.get("fp", "chrome")}
        if security == "reality":
            tls.update(publicKey=q["pbk"], shortId=q.get("sid", ""), spiderX=q.get("spx", ""))
        if q.get("alpn"):
            tls["alpn"] = q["alpn"].split(",")
        stream[security + "Settings"] = tls
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{"listen": "127.0.0.1", "port": 1080, "protocol": "mixed"}],
        "outbounds": [
            {
                "tag": "proxy",
                "protocol": "vless",
                "settings": {
                    "address": v.host,
                    "port": v.port,
                    "id": v.uuid,
                    "encryption": "none",
                    "flow": q.get("flow", ""),
                },
                "streamSettings": stream,
            }
        ],
    }


def _singbox(v: Vless):
    q = v.options
    transport = q.get("type", "tcp")
    if q.get("support-x25519mlkem768") in {"true", "1"}:
        raise Unsupported(
            "Этот вариант Reality требует ML-KEM; используйте актуальное Xray-приложение"
        )
    if transport == "xhttp" or q.get("spx") or q.get("authority"):
        raise Unsupported("Этот профиль требует Xray; выберите приложение с ядром Xray")
    node = {
        "type": "vless",
        "tag": "proxy",
        "server": v.host,
        "server_port": v.port,
        "uuid": v.uuid,
        "flow": q.get("flow", ""),
    }
    if q.get("security", "none") != "none":
        tls = {"enabled": True, "server_name": q.get("sni", v.host)}
        if q.get("fp") or q.get("security") == "reality":
            tls["utls"] = {"enabled": True, "fingerprint": q.get("fp", "chrome")}
        if q.get("alpn"):
            tls["alpn"] = q["alpn"].split(",")
        if q.get("security") == "reality":
            tls["reality"] = {"enabled": True, "public_key": q["pbk"], "short_id": q.get("sid", "")}
        node["tls"] = tls
    if transport not in {"tcp", "raw"}:
        node["transport"] = {"type": transport}
        if transport == "grpc":
            if q.get("host") or q.get("path"):
                raise Unsupported("Неоднозначные параметры gRPC; используйте исходную ссылку")
            node["transport"]["service_name"] = q.get("serviceName", "")
        else:
            node["transport"]["path"] = q.get("path", "/")
            if q.get("host"):
                if transport == "ws":
                    node["transport"]["headers"] = {"Host": q["host"]}
                else:
                    node["transport"]["host"] = q["host"]
    return {
        "log": {"level": "warn"},
        "dns": {
            "servers": [
                {"type": "local", "tag": "bootstrap"},
                {
                    "type": "https",
                    "tag": "remote",
                    "server": "1.1.1.1",
                    "server_port": 443,
                    "path": "/dns-query",
                    "detour": "proxy",
                },
            ],
            "final": "remote",
        },
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "address": ["172.19.0.1/30", "fdfe:dcba:9876::1/126"],
                "auto_route": True,
                "strict_route": True,
                "stack": "system",
            },
            {"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": 1080},
        ],
        "outbounds": [node],
        "route": {
            "final": "proxy",
            "auto_detect_interface": True,
            "default_domain_resolver": "bootstrap",
            "rules": [{"protocol": "dns", "action": "hijack-dns"}],
        },
    }


def _mihomo(v: Vless):
    q = v.options
    network = q.get("type", "tcp")
    if network in {"xhttp", "httpupgrade"} or q.get("spx") or q.get("authority"):
        raise Unsupported("Дополнительные настройки этого профиля требуют приложения с ядром Xray")
    node = {
        "name": "VPN",
        "type": "vless",
        "server": v.host,
        "port": v.port,
        "uuid": v.uuid,
        "udp": True,
        "tls": q.get("security", "none") != "none",
    }
    if q.get("flow"):
        node["flow"] = q["flow"]
    if node["tls"]:
        node.update(servername=q.get("sni", v.host))
        if q.get("fp") or q.get("security") == "reality":
            node["client-fingerprint"] = q.get("fp", "chrome")
        if q.get("alpn"):
            node["alpn"] = q["alpn"].split(",")
        if q.get("security") == "reality":
            node["reality-opts"] = {"public-key": q["pbk"], "short-id": q.get("sid", "")}
            if q.get("support-x25519mlkem768"):
                node["reality-opts"]["support-x25519mlkem768"] = q["support-x25519mlkem768"] in {
                    "true",
                    "1",
                }
    if network not in {"tcp", "raw"}:
        node["network"] = network
        if network == "grpc":
            if q.get("host") or q.get("path"):
                raise Unsupported("Неоднозначные параметры gRPC; используйте исходную ссылку")
            node["grpc-opts"] = {"grpc-service-name": q.get("serviceName", "")}
        if network == "ws":
            node["ws-opts"] = {"path": q.get("path", "/")}
            if q.get("host"):
                node["ws-opts"]["headers"] = {"Host": q["host"]}
    return {
        "mixed-port": 7890,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "warning",
        "proxies": [node],
        "proxy-groups": [{"name": "PROXY", "type": "select", "proxies": ["VPN"]}],
        "rules": ["MATCH,PROXY"],
    }


def _stash(v: Vless):
    # Stash uses sni, rather than Mihomo's servername. Keep its profile independent.
    result = _mihomo(v)
    node = result["proxies"][0]
    if "servername" in node:
        node["sni"] = node.pop("servername")
    # Client listeners and system integration are managed by the Stash app.
    for key in ("mixed-port", "allow-lan", "bind-address"):
        result.pop(key)
    return result


def _loon(v: Vless):
    q = v.options
    transport = q.get("type", "tcp")
    if q.get("support-x25519mlkem768") in {"true", "1"}:
        raise Unsupported("Для этого варианта Reality используйте актуальное Xray-приложение")
    if transport not in {"tcp", "raw", "ws"} or q.get("spx") or q.get("alpn"):
        raise Unsupported("Для этого профиля выберите приложение с ядром Xray")
    if q.get("fp") and q["fp"] != "chrome":
        raise Unsupported("Этот TLS fingerprint не поддерживается экспортом Loon")

    def quote(value):
        if '"' in value or "\\" in value:
            raise Unsupported("Параметры требуют импорта исходной ссылки")
        return '"' + value + '"'

    parts = [
        "VLESS",
        quote(v.host),
        str(v.port),
        quote(v.uuid),
        "transport=" + ("tcp" if transport == "raw" else transport),
        "udp=true",
    ]
    if q.get("security", "none") != "none":
        parts.extend(
            ["over-tls=true", "skip-cert-verify=false", "sni=" + quote(q.get("sni", v.host))]
        )
    if q.get("security") == "reality":
        parts.extend(["public-key=" + quote(q["pbk"]), "short-id=" + quote(q.get("sid", ""))])
    if q.get("fp"):
        parts.append("tls-profile=chrome")
    if q.get("flow"):
        parts.append("flow=" + q["flow"])
    if transport == "ws":
        parts.append("path=" + quote(q.get("path", "/")))
        if q.get("host"):
            parts.append("host=" + quote(q["host"]))
    return "[Proxy]\nVPN = " + ",".join(parts) + "\n\n[Rule]\nFINAL,VPN\n"
