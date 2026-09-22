#!/usr/bin/env python3
"""make_fixtures.py — build `fixtures_generated.json` from real captures.

Why fixtures are TRIMMED
========================
A served product page is ~550 KB and almost all of it is the shop app.
The product lives in `__MODERN_ROUTER_DATA__`, and only the component
carrying `product_info` is kept — plus the page's own `basic_info`,
`bot_info` and `waf_decision`, because those are columns.

What is NOT kept, and why it matters here more than in the sibling repos:
a page served THROUGH A SCRAPING BROWSER PROFILE carries that profile's
session. The trim removes it as a side effect of keeping only what is
under test, which is the version of this that cannot rot (CLAUDE.md §10).

The CHALLENGE fixture is kept whole and small: it is 11 KB, it carries no
session at all, and its entire purpose is to be counted against.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from output_writer import ShopProduct                      # noqa: E402
from product_parser import (parse_product, router_data,     # noqa: E402
                            _page_node, _product_component)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures_generated.json")

SERVED = {
    "shop_served.html": ("product",
                         "a product page TikTok Shop actually served, "
                         "through a Scraping Browser profile"),
    "shop_served2.html": ("product_second_fetch",
                          "the same product fetched again — the prices "
                          "MOVED between the two (100.80 then 94.81), "
                          "which is the site and not a parser"),
}
CHALLENGES = {
    "shop_pdp_browser.html": ("challenge_rendered",
                              "the slide puzzle as a browser renders it"),
    "shop_view_product_dc.html": ("challenge_raw",
                                  "the same refusal as a plain HTTP client "
                                  "sees it: 5,724 bytes, no widget yet"),
}


def _slim_description(model: dict) -> None:
    """Drop what `description_text()` does not read.

    A description is a JSON string of blocks. The parser keeps the `text`
    ones and discards the `image` ones — and those image blocks carry a
    CDN `uri` whose path segment is 32 hex characters, which this repo's
    own credential scan (rightly) flags.

    CLAUDE.md §24 asks the question in the right order: before exempting a
    value, check whether it is needed at all. It is not — nothing reads
    it — so the image blocks keep their `type` (which is what the filter
    under test acts on) and lose their payload. Removing beats forgiving,
    because an exemption is a hole a real key could later hide in.
    """
    raw = model.get("description")
    if not isinstance(raw, str) or not raw.startswith("["):
        return
    try:
        blocks = json.loads(raw)
    except ValueError:
        return
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if isinstance(block, dict) and block.get("type") != "text":
            for key in list(block):
                if key != "type":
                    block.pop(key, None)
    model["description"] = json.dumps(blocks, ensure_ascii=False)


# Fields nothing in this repo reads, removed from the fixtures rather than
# exempted from the credential scan.
#
# `uri` is TikTok's internal object key — "tos-alisg-i-…/<32 hex>" — and
# it sits beside the `url_list` the parser actually reads. It has no
# scheme and no host, so the scan's "inside a TikTok URL" rule cannot see
# it, and it is 32 hex characters, so the scan flags it.
#
# CLAUDE.md §24, for the third time in this build: before exempting a
# value, check whether it is needed. It is not.
_UNREAD_IMAGE_FIELDS = ("uri", "thumb_url_list", "url_prefix")


def _strip_unread(node):
    """Remove `uri` everywhere in the trimmed payload.

    RECURSIVE, because the field turns up at four depths — on a product
    image, inside a description block, on a shop logo and on a video
    cover — and removing it at the one depth that was flagged first left
    the others to be flagged next.

    Nothing in `product_parser.py` reads `uri`: image URLs come from
    `url_list`. Verified with a grep before this function was written,
    which is the order CLAUDE.md §24 asks for — establish that the value
    is unneeded, then remove it, rather than exempting it and hoping.
    """
    if isinstance(node, dict):
        for key in _UNREAD_IMAGE_FIELDS:
            node.pop(key, None)
        for value in node.values():
            _strip_unread(value)
    elif isinstance(node, list):
        for value in node:
            _strip_unread(value)


def _slim_images(model: dict) -> None:
    _strip_unread(model)


def trim(html: str) -> dict:
    data = router_data(html)
    page = _page_node(data)
    component = _product_component(page)
    model = (component.get("product_info") or {}).get("product_model")
    if isinstance(model, dict):
        _slim_description(model)
        _slim_images(model)
    slim_page = {k: page.get(k) for k in
                 ("basic_info", "bot_info", "waf_decision") if k in page}
    slim_page["page_config"] = {"components_map": [{"component_data": component}]}
    payload = {"loaderData":
               {"(region)/pdp/(product_name_slug$)/(product_id)/page":
                slim_page}}
    # Applied to the WHOLE trimmed payload, not to `product_model` alone:
    # the field also turns up on the shop logo and the seller block, and
    # cleaning one branch just moved which line the scan flagged.
    _strip_unread(payload)
    return payload


def as_page(payload: dict) -> str:
    return ('<!DOCTYPE html><html><head><script type="application/json" '
            'id="__MODERN_ROUTER_DATA__">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script></head><body></body></html>")


def build(capture_dir: str) -> dict:
    out = {"_readme": (
        "Generated by make_fixtures.py from real captures. The served "
        "fixtures are the product component of __MODERN_ROUTER_DATA__, "
        "trimmed out of a ~550 KB page; the session the page was fetched "
        "with lives outside that component and is therefore absent rather "
        "than redacted. The challenge fixtures are kept whole because they "
        "are small and carry no session."), "served": {}, "challenges": {}}
    missing = []
    for filename, (name, why) in SERVED.items():
        path = os.path.join(capture_dir, filename)
        if not os.path.exists(path):
            missing.append(filename); continue
        html = open(path, encoding="utf-8", errors="replace").read()
        out["served"][name] = {"why": why, "payload": trim(html)}
    for filename, (name, why) in CHALLENGES.items():
        path = os.path.join(capture_dir, filename)
        if not os.path.exists(path):
            missing.append(filename); continue
        out["challenges"][name] = {
            "why": why,
            "html": open(path, encoding="utf-8", errors="replace").read(),
        }
    if missing:
        print(f"[!] {len(missing)} capture(s) not found: {missing}",
              file=sys.stderr)
    return out


def verify(capture_dir: str) -> int:
    data = json.load(open(OUT, encoding="utf-8"))
    bad = 0
    for filename, (name, _why) in SERVED.items():
        path = os.path.join(capture_dir, filename)
        entry = data["served"].get(name)
        if entry is None or not os.path.exists(path):
            print(f"  {name:22} SKIP"); continue
        original = open(path, encoding="utf-8", errors="replace").read()
        a, da = parse_product(original, "u", "T", ShopProduct)
        b, db = parse_product(as_page(entry["payload"]), "u", "T", ShopProduct)
        same = ([asdict(r) for r in a] == [asdict(r) for r in b]
                and da.get("status") == db.get("status"))
        print(f"  {name:22} {'OK' if same else 'DIFFERS'}  ({len(a)} row(s))")
        bad += 0 if same else 1
    return 1 if bad else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--captures", default="/home/petr/2scraper/captures/tiktok")
    p.add_argument("--verify", action="store_true")
    args = p.parse_args()
    if args.verify:
        sys.exit(verify(args.captures))
    data = build(args.captures)
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"[+] {len(data['served'])} served + {len(data['challenges'])} "
          f"challenge fixture(s) -> {OUT} ({os.path.getsize(OUT):,} bytes)")
