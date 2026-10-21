"""Check official sources and produce an honest documentation/manual QA checklist."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from vpnbot.catalog import OFFICIAL_GUIDES, RECIPES, snapshot


def review(app_id):
    app = next(e for e in snapshot()["entries"] if e["app_id"] == app_id)
    url = OFFICIAL_GUIDES.get(app_id) or next(iter(app["sources"][1:]), app["download_url"])
    if url.startswith("https://github.com/") and len(url.rstrip("/").split("/")) == 5:
        path = url.removeprefix("https://github.com/")
        check_url = "https://raw.githubusercontent.com/" + path + "/HEAD/README.md"
    else:
        check_url = url
    status = "unavailable"
    contains_vless = False
    try:
        req = Request(check_url, headers={"User-Agent": "vpn-bot-doc-review/1"})
        with build_opener(ProxyHandler({})).open(req, timeout=12) as response:
            content = response.read(700_000).decode(errors="replace")
            status = str(response.status)
            contains_vless = "vless" in content.lower()
    except HTTPError as exc:
        status = str(exc.code)
    except OSError:
        status = "transport-error"
    return {
        "app_id": app_id,
        "name": app["name"],
        "recipe": RECIPES[app_id][0],
        "official_source": url,
        "checked_url": check_url,
        "http_status": status,
        "mentions_vless": contains_vless,
        "gui_import_verified": False,
    }


def main():
    with ThreadPoolExecutor(max_workers=6) as pool:
        entries = list(pool.map(review, sorted(RECIPES)))
    root = Path(__file__).resolve().parents[1] / "docs"
    root.mkdir(exist_ok=True)
    (root / "client-review.json").write_text(
        json.dumps(
            {
                "checked_at": datetime.now(UTC).date().isoformat(),
                "note": "Documentation checks are not GUI or end-to-end import verification.",
                "entries": entries,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    for row in entries:
        print(row["app_id"], row["http_status"], "vless-in-source:", row["mentions_vless"])


if __name__ == "__main__":
    main()
