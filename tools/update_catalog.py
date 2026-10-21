"""Refresh the reviewed client/platform snapshot; runtime never scrapes websites."""

import json
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

SOURCE = "https://docs.rw/clients"


class Cards(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards = []
        self.depth = 0
        self.anchor = None
        self.title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class", "")
        if tag == "div":
            if "clientCard_" in cls:
                self.card = {"entry_id": attrs["id"], "core": attrs["data-core"]}
                self.depth = 1
                self.card["sources"] = [SOURCE + "#" + attrs["id"]]
            elif self.depth:
                self.depth += 1
        if self.depth and tag == "a":
            url = attrs.get("href", "")
            self.title = "clientAnchor_" in cls
            if self.title:
                self.card["name"] = ""
            if url.startswith("https://"):
                if "downloadLink_" in cls:
                    self.card["download_url"] = url
                elif not url.startswith("https://t.me/"):
                    self.card["sources"].append(url)

    def handle_data(self, text):
        if self.depth and self.title:
            self.card["name"] += text

    def handle_endtag(self, tag):
        if tag == "a":
            self.title = False
        if tag == "div" and self.depth:
            self.depth -= 1
            if self.depth == 0:
                entry = self.card
                entry["app_id"], entry["platform"] = entry["entry_id"].rsplit("-", 1)
                entry["app_id"] = entry["app_id"].lower()
                self.cards.append(entry)


def main():
    parser = Cards()
    # Explicitly bypass all environment proxy settings.
    with build_opener(ProxyHandler({})).open(SOURCE, timeout=30) as response:
        parser.feed(response.read().decode())
    assert len(parser.cards) >= 90, "Unexpected catalogue shape; review before replacing snapshot"
    result = {
        "source": SOURCE,
        "reviewed_at": datetime.now(UTC).date().isoformat(),
        "entries": sorted(parser.cards, key=lambda e: (e["platform"], e["name"].casefold())),
    }
    path = Path(__file__).resolve().parents[1] / "vpnbot" / "clients.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"Saved {len(parser.cards)} client/platform entries; review import recipes separately")


if __name__ == "__main__":
    main()
