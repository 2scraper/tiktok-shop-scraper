# tiktokshop-scraper

Extract TikTok Shop product listings — price and original price, the
discount, units sold, rating and review count, the seller and their shop
statistics, images, shipping fee and package dimensions.

**This is the one repo in the tiktok-* family where a paid product is the
access rather than a convenience.** Its three siblings say you need none
of them and mean it. This one says the opposite, and the measurement is
below.

---

## What it took to get a single product page

TikTok Shop's web surface is behind TikTok's own slide-puzzle captcha —
a "Security Check" page of about 11 KB served under HTTP 200. Measured
**2026-09-22**, every combination available:

| client | exit | result |
|---|---|---|
| `curl` | Hetzner datacentre | Security Check |
| `curl` | US residential | Security Check |
| headless Chromium | Hetzner datacentre | Security Check |
| headful Chromium | Hetzner datacentre | Security Check |
| headless Chromium | US residential (Comcast, Washington) | Security Check |
| **2Captcha Scraping Browser**, fresh profile | US | Security Check |
| **2Captcha Scraping Browser**, profile that had already visited tiktok.com | US | **SERVED — 3 of 3 product pages** |

A Scraping Browser `pid-` profile keeps its cookies between connections.
Once it has been through `tiktok.com` it stays served: a later run that
connected cold and went straight to a product was served 3 of 3 on a
profile already carrying ~30 cookies. A local browser warmed exactly the
same way reached 11 cookies and was still refused.

So `--cdp-endpoint` is what this repo asks for, and `--warm` (on by
default) is what gets a **new** profile there — two page loads on
tiktok.com before the first product.

```bash
./venv/bin/python playwright_scraper.py \
    --url 1732432759321694958 \
    --cdp-endpoint "ws://{login}-zone-scraping_browser-country-us-pid-{profileId}:{password}@cb.2captcha.com:9222"
```

Without `--cdp-endpoint` the run **warns and tries anyway**, and will
almost certainly exit 3. It is a warning rather than a refusal on purpose:
a transport that refuses to try can never report that the site has
changed.

---

## What is NOT claimed here about the captcha

This matters enough to state precisely, because the easiest thing to write
would be wrong.

* **2Captcha does implement a TikTok captcha method.** Verified by calling
  it: `method=tiktok` returns its own error (`ERROR_TIKTOK`, "one of the
  parameters is incorrect") while invented names like `tiktok_captcha`
  fall through to the generic image path. The product page lists it at
  **€2.8 per 1000**.
* **This repo does not solve the shop's challenge with it.** Every
  parameter combination tried was rejected, and the vendor's documented
  way of discovering `aid` and `host` — redefining `renderCaptcha` in the
  console — does not apply here: the shop's SDK (`oec-ttweb-captcha`)
  never calls that function. Its verification endpoint is
  `api-verification.tiktokshop.com` with `aid=0`.
  `--captcha-aid` and `--captcha-host` exist so that working values can be
  supplied without a code change.
* **The Scraping Browser's auto-solve extension does not cover it
  either.** It injected all sixteen of its hunters into the challenge page
  — turnstile, recaptcha, arkose, geetest, amazon_waf and the rest — and
  left ByteDance's widget untouched. `Captcha.setAutoSolve` returned `{}`
  and no `Captcha.solveFinished` event fired, across three shop routes
  with waits up to 35 seconds.

No sentence here says the captcha cannot be solved. That would be a claim
about a vendor, and the only claim a repo is entitled to is about itself.
**No money was spent** establishing any of the above: every rejected task
was rejected before creation.

---

## The marker that cost three live runs

Worth reading if you maintain anything in this family.

`oec-ttweb-captcha` was in the shared marker set, adopted on a count of 25
captures — 0 on every served page, 2 on the challenge — and every one of
those captures came from `tiktok.com`, because at the time **no served
TikTok Shop page existed to count it on**.

Once one did, the count inverted. Across 5 served shop pages and 4
challenge pages:

| marker | served | challenge |
|---|---:|---:|
| `oec-ttweb-captcha` | **5** | 8 |
| `secsdk-captcha` | 0 | 12 |
| `captcha-init` | 0 | 4 |
| `captcha_verify_img_slide` | 0 | 3 |
| `captcha_verify_container` | 0 | 3 |

The shop loads its captcha SDK on **every** page, ready to fire. So the
loader's name is a fact about the route, not a signal about the response
— and while it was carried, this engine reported exit 3 on a 240 KB page
holding the product it had asked for.

The rule ("count a marker on a page you know is good") did not fail
because it was ignored. It failed because the good page did not exist yet.
A marker is only as verified as the corpus it was counted against, and a
new route is a new corpus.

---

## What you get

39 columns. The ones worth naming:

| column | note |
|---|---|
| `price`, `original_price`, `currency` | from TikTok's own price object, which states the currency as a **fact** rather than leaving it to a symbol |
| `discount_pct` | TikTok's own figure, **checked** against the two prices — where they disagree by more than a point the column is null and the disagreement goes in the sidecar |
| `sold_count`, `review_count`, `rating` | a product with no reviews has **null** rating and null count, never a rating of zero |
| `shop_name`, `shop_rating`, `shop_sold_count`, `shop_review_count`, `shop_followers`, `shop_product_count`, `shop_url` | the seller behind the listing |
| `description` | **text**, not the block JSON TikTok publishes — written through raw it looks populated and is unreadable |
| `image_urls`, `image_count`, `video_count`, `sku_count`, `sale_properties` | the listing's media and its variant axes as TikTok names them |
| `shipping_fee`, `package_weight_g`, `package_*_cm` | the fee lives under an opaque warehouse-id key and is found by walking, not by naming |
| `site_says_bot`, `site_risk_level` | **TikTok's own verdict on your request.** The page's loader data states whether its bot detection classified the fetch as a bot, and at what risk level. Most sites leave that to be inferred; this one says it outright |

### Prices move

Two fetches of the same listing minutes apart returned **100.80** and then
**94.81**. That is the site, not the parser — so `diff_runs.py` reporting a
price change here is ordinary rather than alarming, and the offline suite
pins that the two fixtures really do differ.

---

## What this repo does not do

* **Shop search.** `/shop/s/{query}` redirects away to the main site with
  `enter_method=not_supported_region` — measured from a US exit as well as
  from a datacentre. Refused with that reason rather than returning
  nothing.
* **A Scraper API path.** The measured access is a *persistent* Scraping
  Browser profile; the Scraper API is a different 2Captcha product that
  fetches a URL per call. That is what this repo does not implement, not a
  limitation of the service.

---

## Exit codes

| code | meaning |
|---|---|
| 0 | ok |
| 1 | crash |
| 2 | bad usage |
| 3 | blocked — the challenge |
| 4 | zero products, including "the shop served a page with no product on it" |
| 5 | the content was never obtained |
| 6 | partial |

**A run that finds nothing writes nothing.** `--allow-empty` is the
opt-out. A challenged run exits 3 and leaves the previous good output in
place — which the first live run of this engine did correctly while the
marker set was still wrong, and that is the contract working.

---

## Configuration

```bash
cp .env.example .env
python3 env_config.py      # prints what was picked up, WITHOUT printing secrets
```

Variables: `TWOCAPTCHA_KEY`, `TIKTOK_CDP_ENDPOINT`, `TIKTOK_PROXY`,
`TIKTOK_URL`. A Scraping Browser profile's credentials live about a day,
so never paste a working endpoint anywhere durable — `.env.example`
documents the shape, not a credential.

---

## The canary

Unlike its three siblings, this repo's canary **skips without a secret**
and says so with a `::notice::`. It cannot be otherwise: the access is a
Scraping Browser profile, and a canary that ran without one would be red
every morning — which teaches everyone to ignore checks.

---

## Contributing

```bash
python3 smoke_test.py       # the offline suite — no network, no browser needed
python3 -m pytest
```

---

## Licence

MIT. See `LICENSE`.

Captcha solving, the Scraping Browser API, proxies and fingerprints are
four separately-billed [2Captcha](https://2captcha.com) products behind
one key. This repo needs the **Scraping Browser**, and the table at the top
is why.
