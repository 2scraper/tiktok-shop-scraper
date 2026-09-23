# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count and lists any group it skipped because an engine
library is absent.

**The suite must pass with no engine installed at all.** CI installs only the
core requirements, so any import of `playwright_scraper`, `puppeteer_scraper`
or `selenium_scraper` in a check sits inside `try/except ImportError` with the
skip recorded. If the suite fails on a clean clone, that is itself the bug —
say so.

## Never commit a credential

`.env` and every `.env.*` variant except `.env.example` are in `.gitignore`.
Keep them there.

The engines mask `user:pass@` in their own log lines, but three things are
**not** masked: raw page dumps (`--dump-html`), the Scraper API's `x-debug`
response header, and your shell history. Before pasting output into an issue
or a PR, replace keys, proxy passwords and full `ws://user:pass@host:9222`
endpoints with `***`.

CI fails the build if something credential-shaped is committed. That is a
backstop, not a review.

## Reporting a site change

This repo reads TikTok Shop products from `shop.tiktok.com`, which is behind TikTok's own slide-puzzle captcha for every client tried, and served only through a 2Captcha Scraping Browser profile that has already visited tiktok.com.

The parser reads one structured source and never the rendered DOM:

    `<script id="__MODERN_ROUTER_DATA__">` → `loaderData[…].page_config.components_map[*].component_data.product_info`, found by shape and never by index

So a site change almost always shows up as that source moving or its shape
changing, and the most useful thing a report can carry is the source itself
from a dump — there is an issue template for exactly that.

## What the checks pin, and why

Each of these cost real time when it was found, and the offline suite pins it
so a PR that undoes one fails rather than silently regressing:

- **The captcha SDK's loader is not a marker.** `oec-ttweb-captcha` appears on 5 of 5 served shop pages — the SDK loads on every page, ready to fire. While it was a marker, this repo reported exit 3 on a 240 KB page holding the product. Count any new marker on a SERVED shop page before adding it; tiktok.com pages are a different corpus.

- **Warming is a dwell, not a readiness wait.** The shared readiness selector ends in `body`, which matches instantly, so a readiness wait warms nothing. A profile warmed that way was challenged; one that dwelt on the origin was served.

- **The challenge appears first and may resolve itself.** The settle loop waits while the page is still the challenge, not merely while it is unreadable. A loop that stopped at the first readable state read the challenge on a session about to be served.

- **This repo does not know the parameters 2Captcha's TikTok method needs for the shop.** `method=tiktok` exists — it answers with its own `ERROR_TIKTOK` — but the vendor's way of finding `aid` and `host` hooks `renderCaptcha`, which the shop's SDK never calls. That is what this repo does not implement, never a claim that the captcha cannot be solved.

- **Prices move.** Two fetches of one listing minutes apart gave 100.80 and 94.81. A price change in `diff_runs.py` is ordinary here.

Before adding a challenge marker, count it on a page you **know** was served.
A marker that matches every page is worse than no marker.

## Before a release

```bash
python3 smoke_test.py
python3 .github/ci_checks.py --history-check
```

The second applies the credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. A commit on top cannot reach what a
published tag already holds.

Unlike its three siblings, the canary here **skips without a secret** and says so: the access is a Scraping Browser profile, and a canary running without one would be red every morning and would test nothing.

## Pull requests

Add a check for the behaviour you are changing. `smoke_test.py` is a single
file of plain functions; copy the nearest existing check and edit it. Keep the
three engines identical above their driver layer — a check compares their
public surfaces and flag sets in both directions.
