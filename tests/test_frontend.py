"""Static contracts for the framework-free frontend."""
from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class FrontendParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.links: list[dict[str, str]] = []
        self.scripts: list[dict[str, str]] = []
        self.metas: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if "id" in values:
            self.ids.append(values["id"])
        if tag == "link":
            self.links.append(values)
        elif tag == "script":
            self.scripts.append(values)
        elif tag == "meta":
            self.metas.append(values)


def parsed_frontend() -> tuple[str, FrontendParser]:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    parser = FrontendParser()
    parser.feed(html)
    return html, parser


def test_runtime_dom_hooks_exist_once():
    _, parser = parsed_frontend()
    counts = Counter(parser.ids)
    duplicates = {name for name, count in counts.items() if count > 1}
    assert not duplicates, f"duplicate DOM ids: {sorted(duplicates)}"

    app_js = (WEB / "app.js").read_text(encoding="utf-8")
    runtime_ids = set(re.findall(r"\$\('([^']+)'\)", app_js))
    missing = runtime_ids - counts.keys()
    assert not missing, f"app.js references missing DOM ids: {sorted(missing)}"


def test_page_load_contract():
    html, parser = parsed_frontend()
    viewport = next(meta for meta in parser.metas if meta.get("name") == "viewport")
    assert "width=device-width" in viewport["content"]
    assert "viewport-fit=cover" in viewport["content"]
    assert any(link.get("href") == "style.css" for link in parser.links)
    assert any(script.get("src") == "app.js" for script in parser.scripts)
    assert "fonts.googleapis.com" not in html


def test_responsive_motion_and_local_fonts():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert "@media (min-width: 700px) and (max-width: 1099px)" in css
    assert "@media (min-width: 1100px)" in css
    assert "@media (max-width: 699px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "grain-layer" in css

    font_urls = re.findall(r'url\("(fonts/[^\"]+\.woff2)"\)', css)
    assert len(font_urls) == 3
    for url in font_urls:
        assert (WEB / url).is_file(), f"missing local font: {url}"
