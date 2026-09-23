# Changelog

All notable changes to this project are documented here. Keep a Changelog
format; SemVer as closely as a CLI toolkit can manage.

## [Unreleased]

### Fixed
- **The Scraper API engine failed on every `--wait-text` / `--wait-element` /
  `--wait-state` call, and was billed for it.** It sent `waitFor` as a
  JSON-encoded string; measured 2026-09-23 the live API answers that with
  HTTP 422 "params.waitFor must be an object" and still charges $0.0005,
  while the same request with an object is answered 200. It is now sent as
  an object. (The target status was already read from `http_code`; the new
  regression check pins that too, driving the real `fetch_html` with
  `requests.post` stubbed.)
- **`--wait-state networkidle` is refused by the Scraper API** (HTTP 422
  "params.waitFor.state must be one of: load, domcontentloaded", still
  billed — measured 2026-09-23 on a sibling repo with the object-shaped
  `waitFor`). The choice is removed.

## [0.1.1] — 2026-09-23

> **Correction to v0.1.0.** It described a 2Captcha captcha-solving method
> for TikTok as available and shipped two CLI flags for its parameters.
> That method is deprecated. Both flags are removed (nothing read them),
> and the documentation no longer offers it. The challenge policy
> for the slide puzzle now says `solve: False`, which is what the code
> already did.

## [0.1.0] — 2026-09-22

First release. Reads TikTok Shop product listings.

### The access, measured

TikTok Shop's web surface is behind TikTok's own slide-puzzle captcha.
Measured 2026-09-22, every combination available:

    curl                       datacentre                Security Check
    curl                       US residential            Security Check
    headless Chromium          datacentre                Security Check
    headful Chromium           datacentre                Security Check
    headless Chromium          US residential (Comcast)  Security Check
    Scraping Browser           fresh profile             Security Check
    Scraping Browser           profile that had already
                               visited tiktok.com        SERVED, 3 of 3

A `pid-` profile keeps its cookies between connections. `--warm` (on by
default) is what gets a NEW profile there; a local browser warmed the same
way reached 11 cookies and was still refused.

This is the one repo in the family where a paid product is the access.

### What is NOT claimed

* THIS REPO does not solve the shop's slide puzzle; it implements no
  solver for ByteDance's captcha. The access is a warmed profile.
* The Scraping Browser's auto-solve extension does not cover it either:
  sixteen hunters injected, ByteDance's widget untouched, no
  `Captcha.solveFinished` across three routes and 35-second waits.

### The marker that cost three live runs

`oec-ttweb-captcha` was in the shared marker set, adopted on a count of 25
captures from `tiktok.com` — 0 served, 2 challenge — because no served
TikTok Shop page existed to count it on. Once one did, the count inverted:
**5 on served shop pages against 8 on challenges.** The shop loads its
captcha SDK on every page. While it was carried, this engine reported exit
3 on a 240 KB page holding the product it had asked for.

Removed from `tiktok_payload.py`, which is byte-identical across four
repos — so all four digests move in that commit, which is what the digest
is for.

### Two more bugs this build found in itself

* **Warming was a no-op.** It used the shared readiness primitive, which
  returns the moment its selector matches — and this repo's selector ends
  in `body`. So the "warm" was a navigation and nothing else. Replaced
  with an explicit dwell.
* **The settle loop gave up too early.** It stopped as soon as the state
  was readable, and `challenge` is readable — so it read the challenge on
  a session that was about to be served. It now waits the challenge OUT,
  which is what the successful probe did (13–15 s).

### Also measured

* Prices MOVE: two fetches of one listing minutes apart gave 100.80 and
  94.81.
* The page states TikTok's OWN verdict on the request — `bot_info.is_bot`
  and `basic_info.risk_level` — which most sites leave to be inferred.
  Both are columns.
* The shipping fee lives under an opaque warehouse-id key and is found by
  walking, not by naming; a first attempt returned null on a page that
  plainly states 7.99.
* A description is structured BLOCKS, not text. Written through raw, the
  column holds JSON that looks populated and is unreadable.
* An unreviewed product has null rating and null count, never zero.

### Deliberately not here

* **Shop search.** `/shop/s/{query}` redirects away with
  `enter_method=not_supported_region`, from a US exit as well as a
  datacentre.
* **A Scraper API path.** The access is a persistent browser profile; that
  service fetches a URL per call.
