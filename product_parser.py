"""product_parser.py — this IS the site.

The one route in this family where a paid product is load-bearing
=================================================================
TikTok Shop's web surface is behind TikTok's own slide-puzzle captcha, and
it is behind it for every combination this repo could test. Measured
2026-09-22:

    client                          exit                    result
    curl                            Hetzner datacentre      Security Check
    curl                            US residential          Security Check
    headless Chromium               Hetzner datacentre      Security Check
    headful Chromium                Hetzner datacentre      Security Check
    headless Chromium               US residential (Comcast) Security Check
    2Captcha Scraping Browser       (fresh profile)         Security Check
    2Captcha Scraping Browser       (profile that has
                                     visited TikTok before) SERVED, 547 KB

The last row is the product. A Scraping Browser `pid-` profile keeps
cookies between connections, and once it has been through tiktok.com the
shop serves it: measured 3 of 3 product pages on a profile carrying ~30
cookies, against 0 of 3 on its first visit and 0 of 3 from a local browser
that had been warmed the same way (11 cookies, still refused).

So the honest sentence is the one CLAUDE.md §13 asks for: on THIS route the
2Captcha Scraping Browser is not a convenience, it is the access. The
sibling repos say the opposite about their own routes and mean it.

What is NOT claimed here
========================
CLAUDE.md §19 is explicit that "unsolvable" is a property of a PAGE and
never of a vendor, and that the only sentence a repo may write about a
solver is what the REPO does. So, precisely:

  * 2Captcha DOES implement a TikTok captcha method. Verified by calling
    it: `method=tiktok` returns its own error (`ERROR_TIKTOK`), while
    invented names fall through to the generic image path. The product
    page lists it at EUR 2.8 per 1000.
  * THIS REPO does not solve the shop's challenge with it. Every
    parameter combination tried was rejected, and the vendor's documented
    way of discovering `aid` and `host` — hooking `renderCaptcha` — does
    not apply, because the shop's SDK (`oec-ttweb-captcha`) never calls
    that function. `--captcha-aid` and `--captcha-host` exist so that
    working values can be supplied without a code change.
  * The Scraping Browser's own auto-solve extension does not cover it
    either: it injected all sixteen of its hunters into the challenge page
    and left ByteDance's captcha untouched, `Captcha.setAutoSolve`
    returned {} and no `Captcha.solveFinished` ever fired, across three
    shop routes with waits up to 35 seconds.

Where the data lives
====================
A served product page carries `__MODERN_ROUTER_DATA__` — a React Router
loader payload — with the product under

    loaderData[<route key>].page_config.components_map[N]
        .component_data.product_info

`N` was 3 on the page this parser was written against, and is NOT what
this file anchors on: the component is found by SHAPE, the same reasoning
as CLAUDE.md §4's "anchor on a URL pattern, never a CSS class".
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from tiktok_payload import (BOT_CHALLENGE_MARKERS, PayloadError,
                            decode_page, is_empty_success, waf_markers_present)

logger = logging.getLogger("product_parser")

SOURCE = "shop.tiktok.com"

SHOP_HOST = "shop.tiktok.com"
ACCEPTED_HOSTS = ("shop.tiktok.com", "www.tiktok.com", "tiktok.com")

# A TikTok Shop product id is a 19-digit snowflake. Anchored on the URL,
# which is a contract with search engines, rather than on any class in the
# rendered page (CLAUDE.md §4).
_PRODUCT_ID_RE = re.compile(r"^\d{15,21}$")
_PRODUCT_PATH_RE = re.compile(
    r"/(?:view/product|[a-z]{2}/pdp(?:/[^/]+)?|pdp(?:/[^/]+)?)/(\d{15,21})")

ROUTER_DATA_RE = re.compile(
    r'<script[^>]*id="__MODERN_ROUTER_DATA__"[^>]*>(.*?)</script>', re.S)

# TikTok Shop's own regions, as its URLs spell them.
KNOWN_REGIONS = ("us", "gb", "id", "my", "th", "vn", "ph", "sg", "es", "ie",
                 "it", "de", "fr", "jp", "br", "mx")


class NotAProductUrl(ValueError):
    """Refused WITH THE REASON (CLAUDE.md §5)."""


def parse_target(raw: str) -> str:
    """A product id, a product URL, or say why it is neither."""
    s = (raw or "").strip()
    if not s:
        raise NotAProductUrl("empty target")
    if _PRODUCT_ID_RE.match(s):
        return s
    if "://" not in s:
        s = "https://" + s
    parts = urlsplit(s)
    host = (parts.netloc or "").lower().split(":")[0]
    if host not in ACCEPTED_HOSTS:
        if host.endswith("tiktok.com"):
            raise NotAProductUrl(
                f"{raw!r} is a TikTok host this repo does not read. Products "
                f"live on {SHOP_HOST}; accounts are tiktok-profile-scraper's "
                "and videos are tiktok-video-scraper's.")
        raise NotAProductUrl(
            f"{raw!r} is not on a TikTok host (got {host!r})")
    m = _PRODUCT_PATH_RE.search(parts.path or "")
    if m:
        return m.group(1)
    path = parts.path or "/"
    if path.startswith("/shop/s/") or "/search" in path:
        raise NotAProductUrl(
            f"{raw!r} is a shop SEARCH page. Measured 2026-09-22: that route "
            "redirects away to the main site with "
            "`enter_method=not_supported_region` even from a US exit, so "
            "this repo reads named products rather than pretending to "
            "search.")
    raise NotAProductUrl(
        f"{raw!r} carries no product id. This scraper takes a product id or "
        f"a URL like https://{SHOP_HOST}/view/product/1732432759321694958")


def product_url(product_id: str, region: str = "") -> str:
    if not _PRODUCT_ID_RE.match(str(product_id)):
        raise NotAProductUrl(f"{product_id!r} is not a TikTok Shop product id")
    return f"https://{SHOP_HOST}/view/product/{product_id}"


def page_url(url: str, page: int) -> str:
    """A product page is one page."""
    if page == 1:
        return url
    raise NotAProductUrl(
        "a TikTok Shop product has exactly one page; --pages above 1 is "
        "refused rather than refetching the same product")


# ---------------------------------------------------------------------------
# Reading the payload
# ---------------------------------------------------------------------------

def router_data(html: Any) -> Dict[str, Any]:
    text = decode_page(html)
    m = ROUTER_DATA_RE.search(text)
    if not m:
        raise PayloadError(
            f"no __MODERN_ROUTER_DATA__ script in the page ({len(text)} chars)")
    try:
        return json.loads(m.group(1))
    except ValueError as exc:
        raise PayloadError(f"router payload did not parse: {exc}") from exc


def _page_node(data: Dict[str, Any]) -> Dict[str, Any]:
    """The one loader entry that carries a page config.

    Selected by SHAPE. The route key is a template
    (`(region)/pdp/(product_name_slug$)/(product_id)/page`) that TikTok can
    change without telling anyone, and reconstructing it would be
    anchoring on a build artefact.
    """
    loader = data.get("loaderData") or {}
    for value in loader.values():
        if isinstance(value, dict) and "page_config" in value:
            return value
    raise PayloadError(
        f"router payload carried no page node (keys: {sorted(loader)[:4]})")


def _product_component(page: Dict[str, Any]) -> Dict[str, Any]:
    """The component holding `product_info`, found by shape not by index.

    It was `components_map[3]` on the page this parser was written
    against. Anchoring on 3 would be anchoring on the order TikTok happens
    to render its components in today.
    """
    config = page.get("page_config") or {}
    for component in config.get("components_map") or []:
        if not isinstance(component, dict):
            continue
        data = component.get("component_data")
        if isinstance(data, dict) and "product_info" in data:
            return data
    # The global slot is a second copy, and a fallback rather than the
    # primary: it nests one level deeper and has been seen to lag.
    globals_ = (config.get("global_data") or {}).get("product_info")
    if isinstance(globals_, dict) and "product_info" in globals_:
        return {"product_info": globals_["product_info"]}
    raise PayloadError("no component carried `product_info`")


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
            return float(s)
    return None


def _int(value: Any) -> Optional[int]:
    n = _num(value)
    return int(n) if n is not None else None


def _clean(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s or None


def _bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def shipping_fee(logistic: Dict[str, Any]) -> Optional[float]:
    """The fee, from under an OPAQUE key.

    It lives at `logistic_model.pkg_of_service.{id}.shipping_fee`, where
    `{id}` is a warehouse id that differs per product — so it is found by
    walking one level, never by naming the key. A first attempt read
    `logistic_model.shipping_fee` and returned null on a page that plainly
    states 7.99.
    """
    services = logistic.get("pkg_of_service")
    if not isinstance(services, dict):
        return None
    for service in services.values():
        if not isinstance(service, dict):
            continue
        fee = _num(service.get("shipping_fee"))
        if fee is not None:
            return fee
        for item in service.get("reachable_item_list") or []:
            if isinstance(item, dict):
                fee = _num(item.get("shipping_fee"))
                if fee is not None:
                    return fee
    return None


def description_text(raw: Any) -> Optional[str]:
    """TikTok publishes a description as structured BLOCKS, not as text.

    The field is a JSON string holding a list of `{"type": "text"|"image",
    ...}` objects. Written straight through, a row's `description` is the
    JSON itself — which looks populated, passes any coverage check, and is
    unreadable to the consumer it was collected for.

    The image blocks are dropped and the text blocks joined. A value that
    is not this shape is returned as-is rather than discarded, so a future
    change in the site degrades to the old behaviour instead of emptying
    the column.
    """
    text = _clean(raw)
    if not text or not text.startswith("["):
        return text
    try:
        blocks = json.loads(text)
    except ValueError:
        return text
    if not isinstance(blocks, list):
        return text
    parts = [_clean(b.get("text")) for b in blocks
             if isinstance(b, dict) and b.get("type") == "text"]
    joined = "\n".join(p for p in parts if p)
    return joined or None


def parse_product(html: Any, url: str, scraped_at: str,
                  row_cls: Any) -> Tuple[List[Any], Dict[str, Any]]:
    """One served product page to at most one row."""
    data = router_data(html)
    page = _page_node(data)

    basic = page.get("basic_info") or {}
    bot = page.get("bot_info") or {}
    diag: Dict[str, Any] = {
        # The site's own verdict on the request, which this page states
        # outright — a rarity worth recording beside our own.
        "site_says_bot": _bool(bot.get("is_bot")),
        "site_risk_level": _clean(basic.get("risk_level")),
        "waf_type": (page.get("waf_decision") or {}).get("waf_type"),
    }

    component = _product_component(page)
    info = component.get("product_info") or {}
    model = info.get("product_model") or {}
    product_id = _clean(model.get("product_id"))
    if not product_id:
        diag["status"] = "unavailable"
        return [], diag
    diag["status"] = "ok"

    price_block = (((info.get("promotion_model") or {})
                    .get("promotion_product_price") or {}).get("min_price")
                   or {})
    price = _num(price_block.get("sale_price_decimal"))
    original = _num(price_block.get("origin_price_decimal"))

    # TikTok publishes its own discount. It is CHECKED against the two
    # prices rather than trusted: where the site's figure and the
    # arithmetic disagree by more than a point, the column is nulled
    # rather than a guess being presented as a fact (CLAUDE.md §8), and
    # the disagreement goes in the diagnostics with the id.
    discount = _num(price_block.get("discount_decimal"))
    if discount is not None and discount <= 1:
        discount *= 100.0
    if price is not None and original and original > 0:
        computed = (1.0 - price / original) * 100.0
        if discount is None:
            discount = round(computed, 2)
        elif abs(discount - computed) > 1.0:
            diag["discount_disagreement"] = {
                "product_id": product_id, "site": discount,
                "computed": round(computed, 2)}
            logger.warning("product %s: the site says %.0f%% off and the two "
                           "prices say %.0f%% — leaving discount_pct null "
                           "rather than picking one.",
                           product_id, discount, computed)
            discount = None
    elif original in (None, 0):
        # No original price means no discount, not a discount of zero.
        discount = None

    review = info.get("review_model") or {}
    review_count = _int(review.get("product_review_count"))
    rating = _num(review.get("product_overall_score"))
    # An unreviewed product is not a product rated zero. CLAUDE.md §21,
    # met on a third site in this family — both columns go null together.
    if not review_count:
        review_count, rating = None, None

    shop = component.get("shop_info") or {}
    seller = info.get("seller_model") or {}
    skus = [s for s in (model.get("skus") or []) if isinstance(s, dict)]
    first_sku = skus[0] if skus else {}
    logistic = info.get("logistic_model") or {}

    images = []
    for image in model.get("images") or []:
        if isinstance(image, dict):
            urls = image.get("url_list") or image.get("urlList") or []
            first = next((_clean(u) for u in urls if _clean(u)), None)
            if first:
                images.append(first)
        elif _clean(image):
            images.append(_clean(image))

    sale_props = []
    for prop in model.get("sale_properties") or []:
        if isinstance(prop, dict):
            name = _clean(prop.get("name")) or _clean(prop.get("property_name"))
            if name:
                sale_props.append(name)

    row = row_cls(
        source=SOURCE, scraped_at=scraped_at,
        url=product_url(product_id), sku=product_id,
        title=_clean(model.get("name")),

        product_id=product_id,
        seller_id=_clean(model.get("seller_id")) or _clean(shop.get("seller_id")),
        region=_clean(basic.get("lang")),

        price=price, original_price=original,
        currency=_clean(price_block.get("currency_name")),
        discount_pct=round(discount, 2) if discount is not None else None,
        saving_text=_clean(price_block.get("reduce_price_format")),

        sold_count=_int(model.get("sold_count")),
        review_count=review_count,
        rating=rating,

        shop_name=_clean(seller.get("shop_name")) or _clean(shop.get("shop_name")),
        shop_rating=_num(shop.get("shop_rating")),
        shop_sold_count=_int(shop.get("sold_count")),
        shop_review_count=_int(shop.get("review_count")),
        shop_followers=_int(shop.get("followers_count")),
        shop_product_count=_int(shop.get("on_sell_product_count")),
        shop_url=_clean(shop.get("shop_link")),

        description=description_text(model.get("description")),
        image_urls=images or None,
        image_count=len(images) or None,
        video_count=len(model.get("videos") or {}) or None,
        sku_count=len(skus) or None,
        sale_properties=sale_props or None,

        shipping_fee=shipping_fee(logistic),
        package_weight_g=_int(first_sku.get("package_weight")),
        package_length_cm=_int(first_sku.get("package_length")),
        package_width_cm=_int(first_sku.get("package_width")),
        package_height_cm=_int(first_sku.get("package_height")),
        is_pre_order=_bool(first_sku.get("is_pre_order")),

        site_says_bot=diag["site_says_bot"],
        site_risk_level=diag["site_risk_level"],

        page=1, position=1,
    )
    return [row], diag


# ---------------------------------------------------------------------------
# Page states
# ---------------------------------------------------------------------------

STATE_CONTENT = "content"
STATE_CHALLENGE = "challenge"
STATE_WAF_CHALLENGE = "waf_challenge"
STATE_PRODUCT_UNAVAILABLE = "product_unavailable"
STATE_EMPTY_SUCCESS = "empty_success"
STATE_ERROR = "error"
STATE_PARSE_ERROR = "parse_error"
STATE_UNKNOWN = "unknown"

SITE_ASSET_MARKERS = ("ttwstatic.com", "tiktokcdn.com", "tiktokcdn-eu.com",
                      "tiktokcdn-us.com")
MIN_ASSET_REFERENCES = 2


def asset_reference_count(html: Any) -> int:
    text = decode_page(html)
    return sum(text.count(m) for m in SITE_ASSET_MARKERS)


def challenge_markers_present(html: Any) -> List[str]:
    text = decode_page(html)
    return [m for m in BOT_CHALLENGE_MARKERS if m in text]


def detect_page_state(html: Any, status: Optional[int] = None,
                      url: str = "") -> str:
    """Name what TikTok Shop answered with.

    The argument ORDER is the contract: `detect_page_state(html, status,
    url)` — CLAUDE.md §17, and the suite binds every call site against it.

    The challenge check comes FIRST here, unlike in the sibling repos,
    and that is deliberate: on this route the challenge is the common
    case, and it answers HTTP 200 with a page built out of TikTok's own
    assets — so an asset-count heuristic would call it a served page.
    """
    if is_empty_success(status, html):
        return STATE_EMPTY_SUCCESS

    text = decode_page(html)
    if challenge_markers_present(text):
        return STATE_CHALLENGE
    if waf_markers_present(text):
        return STATE_WAF_CHALLENGE
    if status is not None and status >= 400:
        return STATE_ERROR

    try:
        data = router_data(text)
    except PayloadError:
        data = None
    if data is not None:
        try:
            page = _page_node(data)
            _product_component(page)
            return STATE_CONTENT
        except PayloadError:
            # A shop page this parser understood the frame of, with no
            # product in it: the product is gone, or the id was wrong.
            return STATE_PRODUCT_UNAVAILABLE

    if asset_reference_count(text) >= MIN_ASSET_REFERENCES:
        return STATE_PARSE_ERROR
    return STATE_UNKNOWN
