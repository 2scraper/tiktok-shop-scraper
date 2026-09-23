# Changelog

All notable changes to this project are documented here. Keep a Changelog
format; SemVer as closely as a CLI toolkit can manage.

## [Unreleased]

### Fixed

> **`diff_runs.py` never reported a price change.** Its `TRACKED_FIELDS`
> were tiktok-profile-scraper's account columns, 26 of which `ShopProduct`
> does not have, and `price` was not among them — while the README says a
> price change reported by `diff_runs.py` is ordinary here. It now tracks
> this repo's own columns (price, discount, sales, rating, shop figures,
> shipping). `smoke_test.py` pins every tracked name against the dataclass
> and checks that a changed column is actually reported.

> **A copied `.env.example` set `TIKTOK_URL=nasa`**, a TikTok handle, which
> is not a product. It is now a real product id, and the file says the
> Scraping Browser endpoint is the access here rather than calling a key
> unnecessary because "a profile page is served".

- **Donor prose removed from the shared core.** `output_writer.py`,
  `diff_runs.py`, the engines, `page_flow.py`, `smoke_test.py`,
  `.github/ci_checks.py` and the `Dockerfile` carried text from the repos
  this core was copied from — YouTube comment threads, `--sort top`,
  reply threads, job listings, "the business", `--mode comments --out
  software-engineer` — describing those sites as if they were this one.
  Rewritten from this repo's own README, code and fixtures, or deleted
  where there was no measured equivalent. Explicit sibling provenance
  ("measured on tiktok-profile-scraper's route", "a sibling repo
  (youtube-scraper) had…") is kept and now says whose it is.
- tiktok-ads-scraper leftovers removed: an unused `capture_search_token`
  / `_click_search` pair in all three engines, Selenium's performance-log
  capability that only existed for it, an unused `Advertisement` alias, an
  unused `page_flow.sample_share` with the Ad Library's numbers in it, and
  `SOURCE_DEFAULT`'s comment about the Ad Library.
- `page_flow.py`, the engines and `smoke_test.py` described a profile
  page, an account and `video_unavailable`; they now describe a product
  page. `run_meta`'s test data is a `product` run on `shop.tiktok.com`.
- `.github/ci_checks.py` no longer exempts an `avatar_id` column this repo
  does not have from the credential scan.

- `captcha_solver.py`'s docstring pointed at a "No DataDome solver" section
  that does not exist in this repo (it came with the copied core). Removed.

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
