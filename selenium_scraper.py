#!/usr/bin/env python3
"""selenium_scraper.py — TikTok Shop products.

    python3 selenium_scraper.py --url 1732432759321694958 --cdp-endpoint "ws://..."
    python3 selenium_scraper.py --url "https://shop.tiktok.com/view/product/1732432759321694958"

The one route in this family where a paid product is the access
===============================================================
TikTok Shop's web surface is behind TikTok's own slide-puzzle captcha.
Measured 2026-09-22, every combination available:

    curl,               datacentre               Security Check
    curl,               US residential           Security Check
    headless Chromium,  datacentre               Security Check
    headful Chromium,   datacentre               Security Check
    headless Chromium,  US residential (Comcast) Security Check
    Scraping Browser,   fresh profile            Security Check
    Scraping Browser,   profile that has already
                        visited tiktok.com       SERVED, 3 of 3

The sibling repos in this family say "you need none of the paid products"
and mean it. This one says the opposite, and means that.

Warming, and what it actually is
================================
`_prime_session` visits tiktok.com before the shop. The mechanism is the
PROFILE rather than the warming: a Scraping Browser `pid-` profile keeps
its cookies between connections, so once it has been through the origin it
stays served — a second run that connected cold and went straight to a
product was served 3 of 3 on a profile already carrying ~30 cookies.
Warming is what gets a NEW profile there, and it costs two page loads.

A local browser warmed the same way reached 11 cookies and was still
refused, which is why `--cdp-endpoint` is what the README asks for.

What this repo does NOT claim about the solver
==============================================
CLAUDE.md §19: "unsolvable" is a property of a PAGE, never of a vendor,
and the only sentence a repo may write is what the REPO does.

  * THIS REPO does not solve the shop's slide puzzle. It implements no
    solver for ByteDance's captcha; the access is a warmed profile.
  * The Scraping Browser's auto-solve extension does not cover it either:
    all sixteen hunters injected, ByteDance's captcha untouched, no
    `Captcha.solveFinished` across three routes and 35-second waits.
"""

import argparse
import json
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Imported at MODULE level on purpose — see the note in
# puppeteer_scraper.py and CLAUDE.md §10. smoke_test.py asserts it.
from selenium import webdriver
from selenium.common.exceptions import (WebDriverException,
                                        TimeoutException as SETimeout)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            CaptchaUnsolvable, INJECT_TOKEN_JS,
                            RECAPTCHA_DISCOVERY_JS)
from output_writer import (COMPLETE_STOP_REASONS, ShopProduct,
                           dedupe_by_key, finish_run,
                           utc_now, EXIT_API_ERROR, EXIT_NO_PRODUCTS,
                           SOURCE_DEFAULT)
import page_flow
from page_flow import SolveBudget
from http_transport import HttpSession, TransportError
import product_parser as parser
from product_parser import (NotAProductUrl, STATE_CHALLENGE,
                            STATE_CONTENT, STATE_EMPTY_SUCCESS,
                            STATE_PRODUCT_UNAVAILABLE, STATE_UNKNOWN,
                            detect_page_state, parse_product, parse_target,
                            product_url)
from tiktok_payload import PayloadError
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

# The one name the shared logic below uses for "the driver failed". Each
# engine binds it to its own library's exception, so everything from
# `_prime_session` downwards is byte-comparable across the three — which is
# what makes "the engines must agree" checkable rather than aspirational.
DriverError = (WebDriverException, SETimeout)

MODES = ("product",)
DEFAULT_MODE = "product"

ENGINE_NAME = "selenium"

# Every remote call is bounded (CLAUDE.md §8).
REQUEST_TIMEOUT_MS = 30_000
NAVIGATION_TIMEOUT_MS = 90_000

MIN_CARD_MATCHES = page_flow.MIN_CARD_MATCHES

# Where a session is warmed before the shop. Two loads, and the second is
# a profile page rather than the home page because a home-page-only warm
# reached fewer cookies in testing.
WARM_URLS = ("https://www.tiktok.com/", "https://www.tiktok.com/@nasa")

# How long to sit on each warming page. Not a readiness wait — see
# `_prime_session` for why that is the wrong tool here — but the dwell the
# measured-working probe used.
WARM_DWELL_MS = 5_000


@dataclass
class PageOutcome:
    """One fetch attempt's result, in request order rather than arrival order.

    CLAUDE.md §8: merging by arrival order makes the output depend on which
    worker finished first. Workers return these and the caller sorts.
    """
    number: int
    url: str = ""
    rows: List[Any] = field(default_factory=list)
    state: str = STATE_UNKNOWN
    status: Optional[int] = None
    blocked: bool = False
    error: Optional[str] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    attempted: bool = True


def _mask_credentials(text: Any) -> str:
    """Mask every credential in a string, not just the first.

    CLAUDE.md §8: a masker that handles the first occurrence prints the
    password the other four times and looks like it is working — Playwright
    repeats a CDP endpoint five times in one error, once in the message and
    four more in its call log.
    """
    import re
    out = str(text)
    out = re.sub(r"(?i)\b((?:client)?key|token|api[_-]?key|password)=[^&\s\"']+",
                 r"\1=***", out)
    out = re.sub(r"(wss?://)([^:/@\s]+):([^@\s]+)@", r"\1\2:***@", out)
    return out


def _chrome_ua(chromium_version: str) -> str:
    """A user agent built from the Chromium actually installed.

    CLAUDE.md §8: a hardcoded version drifts from whatever is installed,
    and claiming an older Chrome than the JS engine and TLS handshake
    report is itself a mismatch.
    """
    major = (chromium_version or "").split(".")[0] or "140"
    return (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")


def _proxy_failure(exc: Exception) -> str:
    """Name a dead proxy, or "" for anything else.

    CLAUDE.md §8: Chromium reports a dead proxy as a generic error, not a
    timeout, and the two want opposite responses — a timeout deserves
    another try at the SAME exit, a dead proxy a DIFFERENT one. Catching
    only the timeout type let this escape as a traceback in a sibling repo.
    """
    text = str(exc)
    for marker in ("ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
                   "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_UNEXPECTED_PROXY_AUTH",
                   "ERR_PROXY_CERTIFICATE_INVALID"):
        if marker in text:
            return marker
    return ""


# A `fetch` made from inside the page.
#
# On the site's own origin, so it carries the same cookies, the same proxy
# and the same user agent the browser has — which is the whole reason a
# browser is involved at all. A function EXPRESSION, never an evaluated
# string: TikTok's CSP does carry `unsafe-eval` today (measured
# 2026-09-22), but a CSP is a per-route header a site can tighten without
# notice, and CLAUDE.md §18 records a sibling whose run died on exactly
# that.
_FETCH_JS = """
var spec = arguments[0];
var done = arguments[arguments.length - 1];
var init = {method: spec.method, headers: spec.headers,
            credentials: 'include'};
if (spec.body) { init.body = spec.body; }
fetch(spec.url, init).then(function (response) {
  return response.text().then(function (text) {
    done({status: response.status, text: text});
  });
}).catch(function (err) {
  done({status: null, text: null, error: String(err)});
});
"""


class _BrowserSession:
    """A driver, a page on tiktok.com, and a fetch primitive bound to it.

    A rotation is a FRESH BROWSER (CLAUDE.md §8): cookies a bot manager
    issued against exit A and replayed from exit B are a stronger signal
    than either address alone. So this object is torn down and rebuilt
    rather than having its proxy swapped underneath it.
    """

    def __init__(self, driver, proxy_url: Optional[str],
                 client_version: str, user_agent: Optional[str],
                 owns_driver: bool = True):
        # Always True here: this engine cannot connect to a remote browser
        # at all (see the module docstring), so it only ever ends a driver
        # it started. The flag exists so the three sessions carry the same
        # shape and `check_every_engine_exposes_the_same_public_surface`
        # can say so.
        self.owns_driver = owns_driver
        self.driver = driver
        self.browser = driver
        self.context = driver
        self.page = driver
        self.proxy_url = proxy_url
        self.client_version = client_version
        self.user_agent = user_agent

    # -- transport ---------------------------------------------------------

    def _fetch(self, url: str, method: str = "GET",
               headers: Optional[Dict[str, str]] = None,
               body: Optional[str] = None):
        spec = {"url": url, "method": method, "headers": headers or {},
                "body": body}
        try:
            self.driver.set_script_timeout(REQUEST_TIMEOUT_MS / 1000.0)
            return self.driver.execute_async_script(_FETCH_JS, spec)
        except DriverError as exc:
            raise _TransportError(_mask_credentials(exc)) from exc

    def get_text(self, url: str) -> Tuple[Optional[int], Optional[str]]:
        result = self._fetch(url) or {}
        return result.get("status"), result.get("text")

    def post_json(self, url: str, headers: Dict[str, str],
                  body: Dict[str, Any]) -> Tuple[Optional[int], Any]:
        result = self._fetch(url, "POST", headers, json.dumps(body)) or {}
        status, text = result.get("status"), result.get("text")
        if result.get("error"):
            raise _TransportError(_mask_credentials(result["error"]))
        try:
            return status, json.loads(text) if text else None
        except (TypeError, ValueError):
            # A refusal is not JSON. Hand the body back as text so the
            # classifier can name it rather than the run dying on a decode.
            return status, text

    def goto(self, url: str) -> Optional[int]:
        self.driver.set_page_load_timeout(NAVIGATION_TIMEOUT_MS / 1000.0)
        self.driver.get(url)
        # Selenium reports no HTTP status for a navigation. That is a real
        # gap on sites where the status IS the signal — but not here: every
        # response this engine classifies comes back through `_fetch`,
        # which carries the status from the page's own `fetch`. So nothing
        # is discarded; there is simply nothing to discard at this call.
        return None

    def content(self) -> str:
        try:
            return self.driver.page_source or ""
        except DriverError:
            return ""

    def count_selector(self, selector: str) -> int:
        try:
            return len(self.driver.find_elements(By.CSS_SELECTOR, selector))
        except DriverError:
            return 0

    def evaluate(self, js: str, arg: Any = None) -> Any:
        """Run a function EXPRESSION in the page.

        Wrapped in `return (…)(arg)` because `execute_script` takes a
        function BODY, where the shared captcha module hands out `() =>
        expr`. Never an evaluated string on the page's own terms: a site's
        Content-Security-Policy has no `unsafe-eval` (CLAUDE.md §18), and
        `execute_script` goes through the WebDriver protocol rather than
        through the page's `eval`.
        """
        try:
            if arg is not None:
                return self.driver.execute_script(
                    f"return ({js})(arguments[0]);", arg)
            return self.driver.execute_script(f"return ({js})();")
        except DriverError:
            return None

    @property
    def url(self) -> str:
        try:
            return self.driver.current_url or ""
        except Exception:
            return ""

    def dwell(self, milliseconds: int) -> None:
        time.sleep(milliseconds / 1000.0)

    def enable_auto_solve(self) -> None:
        try:
            self.driver.execute_cdp_cmd("Captcha.setAutoSolve",
                                        {"autoSolve": True})
        except Exception:                                  # noqa: BLE001
            pass

    def cookie_count(self) -> int:
        try:
            return len(self.driver.get_cookies() or [])
        except Exception:                                  # noqa: BLE001
            return 0

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass


class _TransportError(RuntimeError):
    """A transport-level failure, already masked."""


class RemoteBrowserError(RuntimeError):
    """The remote-browser path is unavailable from this driver."""


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------


def _launch_local(pw, args, pool: Optional[ProxyPool]) -> _BrowserSession:
    """A local Chromium, optionally behind one exit from the pool."""
    from proxy_pool import split_credentials

    proxy_url = pool.current if pool else (args.proxy or None)
    options = ChromeOptions()
    if args.headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1366,900")

    if proxy_url:
        host_only, username, _password = split_credentials(proxy_url)
        if username:
            # Said out loud rather than silently dropped. Selenium cannot
            # authenticate a proxy at all, and a user who passed a
            # `user:pass` URL must not be left believing it is doing
            # something (CLAUDE.md §6).
            logger.warning("Selenium cannot authenticate a proxy: the "
                           "credentials in --proxy have been STRIPPED and "
                           "only %s is in use. If this exit needs a "
                           "password, use the Playwright or pyppeteer "
                           "engine.", mask(proxy_url))
        options.add_argument(f"--proxy-server={host_only}")

    user_agent = None
    fingerprint = None
    if args.fingerprint:
        from fingerprint_client import get_fingerprint, fingerprint_user_agent
        fingerprint = get_fingerprint(args.twocaptcha_key, tags=args.fp_tags,
                                      country=args.fp_country)
        user_agent = fingerprint_user_agent(fingerprint)
    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")

    driver = webdriver.Chrome(options=options)
    if not user_agent:
        version = (driver.capabilities or {}).get("browserVersion", "")
        user_agent = _chrome_ua(version)
    if fingerprint is not None:
        _apply_fingerprint(driver, fingerprint, user_agent)
    return _BrowserSession(driver, proxy_url, "",
                           user_agent)


def _apply_fingerprint(driver, fingerprint, user_agent) -> None:
    """Give the identity everything the fingerprint states, not just a UA.

    `--user-agent=` on the command line is a BARE override: it changes
    `navigator.userAgent` and leaves `navigator.userAgentData` and the
    `Sec-CH-UA` header reporting the real browser. CLAUDE.md §24 measured
    that half-identity being refused where a complete one was served, so
    this engine applies the same set its twins do.

    Best effort throughout — a fingerprint is cover, and no run should die
    because cover was imperfect.
    """
    from fingerprint_client import (user_agent_metadata, accept_language,
                                    playwright_init_script)

    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument",
                               {"source": playwright_init_script(fingerprint)})
    except DriverError as exc:
        logger.warning("Could not install the fingerprint's init script: %s",
                       _mask_credentials(exc))

    metadata = user_agent_metadata(fingerprint)
    if not metadata:
        logger.warning("The fingerprint carried no brand list, so its client "
                       "hints are left alone: a HALF identity is worse than "
                       "none (CLAUDE.md §24).")
        return
    payload = {"userAgent": user_agent, "userAgentMetadata": metadata}
    language = accept_language(fingerprint)
    if language:
        payload["acceptLanguage"] = language
    platform = (fingerprint.get("navigator") or {}).get("platform")
    if platform:
        payload["platform"] = platform
    try:
        driver.execute_cdp_cmd("Network.setUserAgentOverride", payload)
        timezone = (fingerprint.get("intl") or {}).get("timeZone")
        if timezone:
            driver.execute_cdp_cmd("Emulation.setTimezoneOverride",
                                   {"timezoneId": timezone})
    except DriverError as exc:
        logger.warning("Could not apply the fingerprint's client hints (%s) — "
                       "the run continues, but navigator.userAgentData will "
                       "disagree with the user agent.",
                       _mask_credentials(exc))


def _connect_remote(pw, args) -> _BrowserSession:
    """Refused, with the reason — see this module's docstring.

    chromedriver's `debuggerAddress` takes a bare `host:port` and has
    nowhere to put a password, so the 2Captcha Scraping Browser endpoint —
    `ws://{login}-zone-…:{password}@cb.2captcha.com:9222` — cannot be used
    from here. Reporting that plainly is the whole point: the alternative
    is an auth failure several steps away from its cause.
    """
    raise RemoteBrowserError(
        "--cdp-endpoint is not usable from the Selenium engine: "
        "chromedriver's debuggerAddress takes a bare host:port and cannot "
        "carry the endpoint's credentials. Use playwright_scraper.py or "
        "puppeteer_scraper.py for the Scraping Browser API.")


def _open_http(args, pool: Optional[ProxyPool]) -> HttpSession:
    """Allowed, and it will almost certainly be refused by the site.

    Kept rather than removed because "the HTTP transport does not work
    here" is a MEASUREMENT this repo wants to keep taking: if TikTok ever
    stops challenging the shop, a plain HTTP run is how anyone would find
    out, and a transport that refuses to try can never report that.

    What it does do is warn once, with the numbers, so nobody spends an
    afternoon on it.
    """
    proxy_url = pool.current if pool else (args.proxy or None)
    logger.warning(
        "--transport http against TikTok Shop: measured 2026-09-22, plain "
        "HTTP got the Security Check from a datacentre address AND from a "
        "US residential exit, 0 pages served. This is tried rather than "
        "refused so that a change in the site is discoverable, but expect "
        "exit 3.")
    return HttpSession(proxy_url, _chrome_ua(""), "")


def _open_session(pw, args, pool: Optional[ProxyPool]):
    if getattr(args, "transport", "browser") == "http" and not args.cdp_endpoint:
        return _open_http(args, pool)
    session = (_connect_remote(pw, args) if args.cdp_endpoint
               else _launch_local(pw, args, pool))
    if args.proxy_rotate == "per-run" or not pool:
        logger.info("Browser up%s", f" via {mask(session.proxy_url)}"
                    if session.proxy_url else "")
    return session


# ---------------------------------------------------------------------------
# Warming the origin
# ---------------------------------------------------------------------------


def _prime_session(session, args, url: str) -> Optional[int]:
    """Visit tiktok.com before the shop, and say why.

    This is the finding that makes this repo work at all. TikTok Shop
    refused every cold request measured; a 2Captcha Scraping Browser
    profile that had been through tiktok.com was served 3 of 3 product
    pages.

    The mechanism is the PROFILE rather than the warming: a `pid-` profile
    keeps its cookies between connections, so once it has been through the
    origin it stays served — the second run, connecting cold and going
    straight to a product, was served 3 of 3 on a profile already carrying
    ~30 cookies. Warming is what gets a NEW profile there, and it costs two
    page loads.

    It is skipped for the HTTP transport, which has no profile to warm and
    would only pay for two extra requests it cannot use.
    """
    if isinstance(session, HttpSession):
        return None
    # Ask the Scraping Browser to clear challenges for us. Measured: it
    # does NOT cover TikTok Shop's own captcha — all sixteen of its
    # hunters injected and ByteDance's widget untouched — so this is not
    # what gets the page. It is sent because it is free, because the
    # successful probe sent it, and because a future version of the
    # extension covering this captcha should not need a code change here.
    session.enable_auto_solve()

    if not args.warm:
        logger.info("--no-warm: going straight to the shop. Measured, a "
                    "profile that has not been through tiktok.com is "
                    "refused.")
        return None
    status = None
    for warm_url in WARM_URLS:
        try:
            status = session.goto(warm_url)
            # A DWELL, not a readiness wait, and the distinction cost this
            # engine its first two live runs.
            #
            # The shared readiness primitive returns the moment its
            # selector matches, and this repo's selector ends in `body` —
            # which matches instantly. So the "warm" was a navigation and
            # nothing else: the page's own scripts never ran, the cookies
            # TikTok sets from JavaScript never arrived, and the shop
            # challenged a session the probe had been getting served.
            #
            # (Described rather than named, because the check that keeps
            # this fixed greps this function for that primitive's name —
            # and a comment about a banned string is a use of it, which
            # CLAUDE.md §22 records the family learning the hard way.)
            #
            # WARM_DWELL_MS is what the successful probe used.
            session.dwell(WARM_DWELL_MS)
        except DriverError as exc:
            failure = _proxy_failure(exc)
            if failure:
                raise
            logger.warning("Could not warm on %s (%s)", warm_url,
                           _mask_credentials(exc))
    try:
        cookies = session.cookie_count()
        logger.info("Warmed on tiktok.com — the session carries %d cookie(s). "
                    "Measured: ~30 is what a served shop request looked "
                    "like, 11 was not enough.", cookies)
    except Exception:                                      # noqa: BLE001
        pass
    return status


def handle_captcha_if_present(session, args, budget: SolveBudget) -> bool:
    """Route to the browser handler, or say why there is nothing to do.

    The annotation on this used to promise a `_BrowserSession`, which
    stopped being true the moment a transport without a page existed. On
    the HTTP transport there is no document to inject a token into and no
    DOM to detect a widget in, so this returns False and says so ONCE per
    run rather than per page — a warning repeated forty times is a warning
    nobody reads.
    """
    if isinstance(session, HttpSession):
        if args.solve_captcha != "never" and not getattr(
                args, "_http_solve_warned", False):
            args._http_solve_warned = True
            logger.warning("A challenge cannot be solved on the HTTP "
                           "transport: there is no page to inject a token "
                           "into. --transport auto (the default) starts a "
                           "browser when the site refuses.")
        return False
    return _handle_captcha_in_browser(session, args, budget)


def _handle_captcha_in_browser(session, args,
                               budget: SolveBudget) -> bool:
    """Detect and, if it is worth paying for, solve a challenge.

    Both call sites — before classification and after — go through the same
    `SolveBudget`, which is the CLAUDE.md §23 fix: `SOLVES_PER_PAGE` read
    like an enforced limit in every repo in this family and was not one,
    because only the second of the two calls was counted. One page bought
    three solves on a site where a challenge rendered on every fetch.

    A missing key or a solver error is a WARNING and the run continues
    (CLAUDE.md §8): detection is not the same as blocking, and a run that
    already has data must not die because a solve failed.
    """
    if args.solve_captcha == "never":
        return False
    html = session.content()
    if not html:
        return False
    static = detect_recaptcha_v3(html, session.url)
    live = None
    try:
        live = detect_recaptcha_in_page(session.evaluate, session.url)
    except Exception:                              # noqa: BLE001
        live = None
    challenge = reconcile_detections(static, live)
    if challenge is None:
        return False
    if not budget.may_spend():
        logger.warning("A challenge is present and this page's solve budget "
                       "(%d) is already spent — not paying twice for one "
                       "page.", budget.limit)
        return False
    if not args.twocaptcha_key:
        logger.warning("A captcha is present and no --twocaptcha-key was "
                       "given; continuing unsolved. The run reports exit 3 "
                       "if it really was blocked.")
        return False
    if not budget.spend():
        return False
    if args.cdp_endpoint:
        # The token is MINTED over plain HTTPS from this machine and then
        # installed into a browser that is somewhere else entirely. A
        # Scraping Browser endpoint carries a `country-` segment, so the
        # solve can be issued on one continent and replayed from another —
        # and a token a challenge issuer binds to the solving address is
        # then worthless on arrival. Said out loud rather than left to be
        # discovered from a bill: nothing here can fix it, and the remedy
        # is the endpoint's own auto-solve (`Captcha.setAutoSolve`), which
        # runs where the browser is.
        logger.warning("Solving over --cdp-endpoint mints the token from "
                       "THIS machine and installs it into a remote browser, "
                       "so it may be issued on a different exit than the one "
                       "that will use it. If the token is refused, that is "
                       "the likeliest reason.")
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                min_score=args.min_score,
                                api_version=args.captcha_api)
    except CaptchaUnsolvable as exc:
        logger.warning("Captcha not solved: %s", _mask_credentials(exc))
        return False
    except Exception as exc:                      # noqa: BLE001
        logger.warning("Captcha solver failed: %s", _mask_credentials(exc))
        return False
    if session.evaluate(INJECT_TOKEN_JS, token) is None:
        logger.warning("Could not inject the solved token into the page.")
        return False
    logger.info("Captcha solved and token injected.")
    return True


# ---------------------------------------------------------------------------
# One fetch, with the family's retry / block policy around it
# ---------------------------------------------------------------------------


def _dump(args, name: str, payload: Any) -> None:
    """Write the exact bytes a call returned.

    On SUCCESS too, not only on failure (CLAUDE.md §9): a run can return
    the right count with a field silently unpopulated, and then the exact
    payload is the only way to tell a parsing bug from a too-early
    snapshot.
    """
    if not args.dump_html:
        return
    path = f"{args.out}_{name}.json"
    try:
        with open(path, "w", encoding="utf-8") as handle:
            if isinstance(payload, (dict, list)):
                json.dump(payload, handle, ensure_ascii=False)
            else:
                handle.write(str(payload))
        logger.info("Wrote %s", path)
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)


def _call(session, args, url: str, budget: SolveBudget,
          label: str) -> Tuple[Optional[int], Any, str]:
    """GET one product page and classify the answer."""
    if isinstance(session, HttpSession):
        status, text = session.get_text(url)
    else:
        status = session.goto(url)
        # The shop is a client-rendered app AND it shows the challenge
        # first, resolving it in JavaScript afterwards. So a read taken
        # the instant navigation settles catches the challenge on a
        # session that is about to be served — which is exactly what the
        # first live run of this engine did, reporting exit 3 on a profile
        # that had been serving product pages minutes earlier.
        #
        # Measured: the successful fetches waited 13-15 seconds. So the
        # loop waits while the page is STILL the challenge, not merely
        # while it is unreadable, and takes whatever it settles on.
        # Give the app a chance to paint before the first read, through
        # the shared readiness primitive. It is a weak signal here (the
        # selector ends in `body`, so it returns fast) and that is fine:
        # the real waiting is the settle loop below, and this just avoids
        # reading an empty document.
        page_flow.wait_for_count(session.count_selector,
                                 page_flow.ready_selector(args.mode),
                                 page_flow.min_matches(args.mode),
                                 timeout_ms=5_000)
        waiting_states = (STATE_UNKNOWN, parser.STATE_PARSE_ERROR,
                          STATE_CHALLENGE)
        deadline = time.time() + (page_flow.content_timeout_ms(args.mode) / 1000.0)
        text = session.content()
        state = detect_page_state(text, status, url)
        while time.time() < deadline and state in waiting_states:
            time.sleep(1.5)
            text = session.content()
            state = detect_page_state(text, status, url)
    state = detect_page_state(text, status, url)
    logger.debug("%s -> http %s, state %s", label, status, state)
    return status, text, state


def _fetch_with_policy(session_box: Dict[str, Any], pw, args,
                       pool: Optional[ProxyPool], url: str, label: str
                       ) -> Tuple[Optional[int], Any, str, bool]:
    """One call plus the retry / rotate / solve policy around it.

    `session_box` holds the live session so a rotation can replace it: a
    rotation is a fresh browser, never a proxy swapped under a live
    session (CLAUDE.md §8).
    """
    budget = SolveBudget()
    attempts = max(1, int(args.retries) + 1)
    blocked_seen = False
    status = payload = None
    state = STATE_UNKNOWN

    for attempt in range(1, attempts + 1):
        session = session_box["session"]
        # First of the two solve call sites: clear a challenge BEFORE the
        # answer is judged, so a gated page is not classified on its
        # interstitial.
        if args.solve_captcha == "always":
            handle_captcha_if_present(session, args, budget)
        try:
            status, payload, state = _call(session, args, url, budget,
                                           label)
        except (_TransportError, TransportError) as exc:
            state = parser.STATE_ERROR
            payload = str(exc)
            status = None
            exit_failed = _proxy_failure(exc)
            if exit_failed:
                # A dead proxy is NOT a timeout, and the two want opposite
                # responses: a timeout deserves another try at the SAME
                # exit, a dead proxy a DIFFERENT one. Chromium reports it
                # as a generic error rather than as a timeout, which is how
                # this escaped as a traceback in a sibling repo
                # (CLAUDE.md §8).
                logger.warning("%s failed at the EXIT, not at the site: %s "
                               "via %s. Rotating rather than retrying the "
                               "same address.", label, exit_failed,
                               mask(session.proxy_url))
                if pool:
                    pool.advance(exit_failed)
                    session_box["session"].close()
                    session_box["session"] = _open_session(pw, args, pool)
                    _prime_session(session_box["session"], args,
                                   session_box["prime_url"])
            else:
                logger.warning("%s failed after %d attempt(s): %s",
                               label, attempt, exc)

        if page_flow.counts_as_blocked(state):
            blocked_seen = True
            # `auto` means HTTP until the site says otherwise, and this is
            # otherwise. An HTTP client has nowhere to put a solved token,
            # no cookie jar a challenge issuer will accept and no DOM to
            # find a widget in, so the only useful response to a refusal is
            # to stop being an HTTP client.
            #
            # Once per run, and then never again: a site that challenged
            # once will challenge again, and flapping between transports
            # would pay the browser's start-up cost on every page while
            # looking like it was trying something new.
            if (getattr(args, "transport", "auto") == "auto"
                    and isinstance(session_box["session"], HttpSession)):
                logger.warning("%s was refused over plain HTTP (%s) — "
                               "starting a browser and retrying. This is "
                               "what --transport auto is for, and it happens "
                               "once per run.", label, state)
                session_box["session"].close()
                args.transport = "browser"
                session_box["session"] = _open_session(pw, args, pool)
                _prime_session(session_box["session"], args,
                               session_box["prime_url"])
                continue
            # Second call site, same budget.
            if page_flow.should_solve(state):
                handle_captcha_if_present(session, args, budget)

        if not page_flow.should_retry(state) or attempt >= attempts:
            break
        if page_flow.counts_as_blocked(state):
            if not page_flow.RETRY_ON_BLOCKED:
                break
            budget_left = (args.proxy_block_retries if pool
                           else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
            if attempt > budget_left:
                break
            if pool and pool.rotates_per_page():
                pool.advance(f"state {state}")
                logger.info("Rotating exit and rebuilding the browser — a "
                            "rotation is a fresh browser, never a proxy "
                            "swapped under a live session.")
                session_box["session"].close()
                session_box["session"] = _open_session(pw, args, pool)
                _prime_session(session_box["session"], args,
                               session_box["prime_url"])
        logger.info("%s: state %s, retrying (%d/%d) in %.1fs",
                    label, state, attempt, attempts - 1, args.retry_delay)
        time.sleep(args.retry_delay)

    return status, payload, state, blocked_seen


# ---------------------------------------------------------------------------
# Proxy rotation between pages
# ---------------------------------------------------------------------------


def _rotate_if_per_page(session_box, pw, args, pool, why: str) -> bool:
    """Take a new exit between pages, when `--proxy-rotate per-page` asked.

    This is what that mode NAMES and, before this, not what it did:
    `pool.advance()` was reached only from a dead exit or a refusal, so a
    run whose pages all succeeded stayed on one address for its whole
    life. The flag read like a traffic-spreading control and was a
    recovery control — a setting that looks configurable and is not
    (CLAUDE.md §3 says that about `.env`; it is the same defect here).

    A rotation is a FRESH BROWSER (CLAUDE.md §8): cookies a bot manager
    issued against exit A and replayed from exit B are a stronger signal
    than either address alone, so the session is torn down and rebuilt
    rather than having its proxy swapped underneath it.

    Each product is fetched by its own address and nothing one fetch
    receives is an input to the next, so there is no chain to break; the
    rebuilt session is warmed again by `_prime_session`, because on this
    route a cold session is what gets the Security Check.
    """
    if not pool or not pool.rotates_per_page() or len(pool) < 2:
        return False
    pool.advance(why)
    session_box["session"].close()
    session_box["session"] = _open_session(pw, args, pool)
    _prime_session(session_box["session"], args, session_box["prime_url"])
    return True


def _worker_pool(pool: Optional[ProxyPool], worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Each worker gets its OWN pool object holding the same exits rotated to
    a different offset, so workers start on distinct addresses and no
    thread needs a lock — the concurrency is safe by construction rather
    than by discipline (CLAUDE.md §7).
    """
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


# ---------------------------------------------------------------------------
# Targets: products
# ---------------------------------------------------------------------------






def _targets(args) -> List[str]:
    """`--url` to a list of product ids, refusing each bad one by name."""
    out, seen = [], set()
    for part in str(args.url or "").split(","):
        part = part.strip()
        if not part:
            continue
        pid = parse_target(part)
        if pid in seen:
            logger.info("Product %s named twice; fetching it once.", pid)
            continue
        seen.add(pid)
        out.append(pid)
    if not out:
        raise NotAProductUrl("--url named no products")
    return out


def _fetch_one_product(session_box, pw, args, pool, product_id, scraped_at,
                       number) -> PageOutcome:
    url = product_url(product_id)
    status, text, state, blocked = _fetch_with_policy(
        session_box, pw, args, pool, url, f"product {product_id}")
    _dump(args, f"product_{product_id}", text)
    outcome = PageOutcome(number=number, url=url, state=state, status=status,
                          blocked=blocked)

    if not page_flow.should_parse(state):
        if state == STATE_CHALLENGE:
            logger.error(
                "product %s: TikTok Shop served its slide-puzzle captcha. "
                "Measured 2026-09-22, the ONLY configuration that got past "
                "it was a 2Captcha Scraping Browser profile that had "
                "already visited tiktok.com — 3 of 3 product pages served "
                "on a profile carrying ~30 cookies, against 0 of 3 on its "
                "first visit, 0 of 3 from a local browser, and 0 of 3 from "
                "a US residential exit. See the README.", product_id)
        elif state == STATE_PRODUCT_UNAVAILABLE:
            logger.warning(
                "product %s: the shop served a page with no product on it. "
                "The listing is gone, or the id was wrong — this is TikTok "
                "answering, not refusing.", product_id)
        return outcome

    try:
        rows, diag = parse_product(text, url, scraped_at, ShopProduct)
    except PayloadError as exc:
        logger.error("product %s was served and did not parse: %s",
                     product_id, exc)
        outcome.state = parser.STATE_PARSE_ERROR
        outcome.error = str(exc)
        return outcome

    outcome.rows = rows
    outcome.diagnostics = diag
    if not rows:
        outcome.state = STATE_PRODUCT_UNAVAILABLE
    return outcome


def _run_product(session_box, pw, args, pool) -> Tuple[List[Any], Dict[str, Any]]:
    products = _targets(args)
    products = products[:page_flow.pages_to_plan(len(products), len(products))]
    scraped_at = utc_now()

    outcomes = []
    for index, product_id in enumerate(products, start=1):
        if index > 1 and args.delay:
            time.sleep(args.delay)
        if index > 1 and pool and pool.rotates_per_page():
            _rotate_if_per_page(session_box, pw, args, pool,
                                f"before product {product_id}")
        outcomes.append(_fetch_one_product(session_box, pw, args, pool,
                                           product_id, scraped_at, index))

    rows: List[Any] = []
    seen: set = set()
    failed: List[int] = []
    unavailable: List[str] = []
    challenged: List[str] = []
    blocked = False
    verdicts: Dict[str, Any] = {}

    for outcome, product_id in zip(outcomes, products):
        blocked = blocked or outcome.blocked
        if outcome.state == STATE_CHALLENGE:
            challenged.append(product_id)
            failed.append(outcome.number)
            continue
        if outcome.state == STATE_PRODUCT_UNAVAILABLE:
            unavailable.append(product_id)
            continue
        if outcome.rows:
            rows.extend(dedupe_by_key(outcome.rows, seen))
            # The site's own verdict on the request, which this route
            # states outright. Recorded beside our own so a reader can
            # see that TikTok classified the fetch as human even while
            # the run was being challenged elsewhere.
            if outcome.diagnostics.get("site_risk_level"):
                verdicts[product_id] = {
                    "is_bot": outcome.diagnostics.get("site_says_bot"),
                    "risk_level": outcome.diagnostics.get("site_risk_level"),
                }
        else:
            failed.append(outcome.number)

    if challenged:
        stop_reason = "blocked"
        blocked = True
    elif failed:
        stop_reason = "page_failed"
    elif unavailable and not rows:
        stop_reason = "product_unavailable"
    else:
        stop_reason = "completed"

    meta = {
        "stop_reason": stop_reason,
        "pages_completed": len(outcomes) - len(failed),
        "pages_failed": failed or None,
        "blocked": blocked,
        "products_requested": len(products),
        "products_unavailable": unavailable or None,
        "products_challenged": challenged or None,
        "site_verdicts": verdicts or None,
        "used_scraping_browser": bool(args.cdp_endpoint),
        "discount_disagreements": [
            o.diagnostics["discount_disagreement"] for o in outcomes
            if o.diagnostics.get("discount_disagreement")
        ] or None,
    }
    return rows, meta


_RUNNERS = {"product": _run_product}


class _driver_context:
    """The driver's own lifetime, as a context manager.

    Playwright needs one (`sync_playwright()`); Selenium and pyppeteer do
    not, and theirs is a no-op holding the same shape. Keeping it here means
    `scrape()` and the worker loop are identical in all three files.
    """

    def __enter__(self):
        return None            # Selenium needs no driver-level handle

    def __exit__(self, *exc):
        return False


def scrape(args) -> int:
    pool = proxy_pool_from_args(args)
    products = _targets(args)
    if args.concurrency > 1:
        # Products ARE independently addressable — the policy says so —
        # and the cap that actually bites is the Scraping Browser's one
        # live connection per profile, applied just below.
        if page_flow.pagination_is_addressable(products[0], args.mode):
            args.concurrency = page_flow.concurrency_for_mode(args.mode,
                                                              args.concurrency)
    limit = page_flow.concurrency_limit(args.cdp_endpoint)
    if limit and args.concurrency > limit:
        args.concurrency = limit

    if not args.cdp_endpoint:
        # A warning, not a refusal. CLAUDE.md §7's rule for concurrency
        # applies to access too: state the measurement and let the caller
        # decide, because a refusal would also stop anyone discovering
        # that the site had changed.
        logger.warning(
            "No --cdp-endpoint. Measured 2026-09-22, TikTok Shop served 0 of "
            "3 product pages to a local browser (headless and headful), 0 of "
            "3 through a US residential exit, and 3 of 3 through a 2Captcha "
            "Scraping Browser profile that had visited tiktok.com. Expect "
            "exit 3 without one.")

    prime_url = WARM_URLS[0]
    rows: List[Any] = []
    meta: Dict[str, Any] = {}
    with _driver_context() as pw:
        session_box = {"session": _open_session(pw, args, pool),
                       "prime_url": prime_url}
        try:
            _prime_session(session_box["session"], args, prime_url)
            rows, meta = _RUNNERS[args.mode](session_box, pw, args, pool)
        finally:
            session_box["session"].close()

    extra = {k: v for k, v in meta.items()
             if k not in ("stop_reason", "pages_completed", "pages_failed",
                          "blocked")}
    extra["engine"] = ENGINE_NAME
    extra["category"] = args.category

    challenged = meta.get("products_challenged")
    if challenged:
        logger.error(
            "%d of %d product(s) were met with the slide-puzzle captcha: %s. "
            "The measured way past it is a 2Captcha Scraping Browser profile "
            "that has already visited tiktok.com — see the README for what "
            "was tried and what it returned.",
            len(challenged), meta.get("products_requested"),
            ", ".join(challenged[:5]))

    verdicts = meta.get("site_verdicts") or {}
    if verdicts:
        # The site's own opinion of the request, which this route states
        # outright and most do not.
        levels = sorted({v.get("risk_level") for v in verdicts.values()})
        bots = [p for p, v in verdicts.items() if v.get("is_bot")]
        logger.info("TikTok's own verdict on these requests: risk_level %s, "
                    "is_bot true on %d of %d. That is the SITE's opinion, "
                    "recorded beside ours.",
                    "/".join(str(x) for x in levels), len(bots), len(verdicts))

    disagreements = meta.get("discount_disagreements")
    if disagreements:
        logger.warning(
            "%d product(s) publish a discount that does not match their own "
            "two prices; discount_pct is null on those rows rather than "
            "guessing which figure to believe: %s",
            len(disagreements), disagreements[:3])

    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=bool(meta.get("blocked")),
        stop_reason=meta.get("stop_reason", "completed"),
        pages_requested=len(products),
        pages_completed=int(meta.get("pages_completed") or 0),
        pages_failed=meta.get("pages_failed") or None,
        start_url=prime_url, final_url=prime_url,
        mode=args.mode, source=SOURCE_DEFAULT, extra=extra)


def parse_args(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(
        description="Scrape TikTok Shop products. This is the one route in "
                    "the tiktok-* family where a paid product is the access "
                    "rather than a convenience — see the README.")
    p.add_argument("--url", default=None,
                   help="A product id (19 digits) or a product URL, or a "
                        "comma-separated list of them. Falls back to "
                        "TIKTOK_URL.")
    p.add_argument("--mode", choices=MODES, default=DEFAULT_MODE,
                   help="product (default, and the only one). Shop SEARCH is "
                        "not implemented: measured, /shop/s/ redirects away "
                        "with `enter_method=not_supported_region` even from "
                        "a US exit.")
    p.add_argument("--warm", dest="warm", action="store_true", default=True,
                   help="Visit tiktok.com before the shop. On by default, "
                        "and it is what gets a NEW Scraping Browser profile "
                        "served: measured 3 of 3 product pages on a warmed "
                        "profile against 0 of 3 on its first cold visit.")
    p.add_argument("--no-warm", dest="warm", action="store_false",
                   help="Go straight to the shop. Fine on a profile that has "
                        "been used before; refused on a fresh one.")
    p.add_argument("--pages", type=int, default=1,
                   help="Kept for the family's flag contract and capped at "
                        "1: a product page is one page. Name more products "
                        "in --url instead.")
    p.add_argument("--category", default=None,
                   help="Label to tag the run with in the sidecar.")
    p.add_argument("--locale", default="en",
                   help="Kept for the family's flag contract. The shop's own "
                        "region comes from the exit, not from this.")
    p.add_argument("--format", choices=("json", "csv", "both"), default="json")
    p.add_argument("--out", default="tiktok_shop", help="Output file prefix.")
    p.add_argument("--delay", type=float, default=0.0)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--retry-delay", type=float, default=2.0)
    p.add_argument("--concurrency", type=int, default=1,
                   help="Forced to 1 with --cdp-endpoint, which the access "
                        "needs: the Scraping Browser allows one live "
                        "connection per profile and workers collide with "
                        "`profile_locked`. Several pids, one run each.")
    p.add_argument("--proxy", default=None)
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-rotate", choices=ROTATE_MODES, default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2)
    p.add_argument("--twocaptcha-key", default=None,
                   help="2Captcha API key. Also read from TWOCAPTCHA_KEY.")
    p.add_argument("--captcha-api", choices=("v1", "v2"), default="v2")
    p.add_argument("--solve-captcha", choices=("never", "when-blocked", "always"),
                   default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.3)
    p.add_argument("--transport", choices=("auto", "http", "browser"),
                   default="browser",
                   help="browser (default). `http` is allowed and warns: "
                        "measured, plain HTTP got the Security Check from "
                        "every address tried. It is not refused, because a "
                        "transport that refuses to try can never report that "
                        "the site changed.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="ws:// endpoint of the 2Captcha Scraping Browser API. "
                        "Also read from TIKTOK_CDP_ENDPOINT. This is the one "
                        "route in this family where it is the ACCESS rather "
                        "than a convenience.")
    p.add_argument("--fingerprint", action="store_true")
    p.add_argument("--fp-country", default=None)
    p.add_argument("--fp-tags", default="Windows")
    p.add_argument("--dump-html", action="store_true")
    p.add_argument("--allow-empty", action="store_true")
    headless = p.add_mutually_exclusive_group()
    headless.add_argument("--headless", dest="headless", action="store_true",
                          default=True)
    headless.add_argument("--headful", dest="headless", action="store_false")

    args = p.parse_args(argv)
    env_config.apply(args)

    if not args.url:
        p.error("no --url given, and TIKTOK_URL is not set in the "
                "environment or .env.")
    try:
        products = _targets(args)
    except NotAProductUrl as exc:
        p.error(str(exc))

    if args.pages > 1:
        p.error("--pages above 1 is refused: a TikTok Shop product has "
                "exactly one page. Name more products in --url instead.")
    if args.cdp_endpoint and (args.proxy or args.proxy_file):
        p.error("--cdp-endpoint already proxies; attaching --proxy stacks a "
                "second exit and creates a mismatch rather than better cover.")
    if args.concurrency < 1:
        p.error("--concurrency must be at least 1.")
    if args.category is None:
        args.category = products[0]
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key.")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second creates a mismatch rather than "
                       "better cover.")
    try:
        sys.exit(scrape(args))
    except NotAProductUrl as exc:
        logger.error("%s", exc)
        sys.exit(2)
    except ProxyError as exc:
        logger.error("%s", exc)
        sys.exit(2)
