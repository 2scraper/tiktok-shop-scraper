# Changelog

All notable changes to this project are documented here. Keep a Changelog
format; SemVer as closely as a CLI toolkit can manage.

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

* 2Captcha DOES implement a TikTok captcha method — verified by calling
  it, since `method=tiktok` returns `ERROR_TIKTOK` while invented names
  fall through to the generic image path. Listed at €2.8 per 1000.
* THIS REPO does not solve the shop's challenge with it. The vendor's way
  of finding `aid` and `host` hooks `renderCaptcha`, which the shop's SDK
  never calls. `--captcha-aid` / `--captcha-host` exist so working values
  need no code change.
* The Scraping Browser's auto-solve extension does not cover it either:
  sixteen hunters injected, ByteDance's widget untouched, no
  `Captcha.solveFinished` across three routes and 35-second waits.

No money was spent establishing any of this: every rejected task was
rejected before creation.

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
