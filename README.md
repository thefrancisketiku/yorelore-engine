# Lead Finder v3: how to run

```
cd "C:\Users\franc\OneDrive\Desktop\Yorelore - Outreach system\lead-finder"
python lead_finder.py --country US --count 60
python lead_finder.py --country AU --count 30
python lead_finder.py --sync        (re-push the whole tracker to the Google Sheet)
```
Or just tell Claude "run the lead finder: 40 US leads".

**Options:**
- `--niche "candles"` (repeatable; the default is a broad mix)
- `--sources ig,tiktok,etsy,ebay,web`
- `--count`
- `--min-score 3`
- `--max-searches 200` (Serper credit cap)
- `--dry-run`

**Where leads go:**
- `Yorelore lead tracker.csv` (one folder up)
- Google Sheet "Yorelore Lead Tracker": https://docs.google.com/spreadsheets/d/1S1duNcYyiz4waiPyhufH7s8PN-xvKZ052lsPC32SSX4/edit (Leads tab; the 11 Oct Shopify test batch has its own tab)
- `output/` keeps a copy of each run.

**What it does:**
1. Searches Google (Serper) for small sellers on Instagram, TikTok (personal accounts that sell included), Etsy, eBay and independent stores (Shopify, WooCommerce, Wix, Squarespace, Big Cartel, Square). It never logs in to or scrapes those platforms.
2. Size gate:
   - Instagram: 2k–150k followers
   - Etsy: 1k–80k sales
   - eBay: 1k+ items sold
   - Stores: a real store (5–800 products, or an ad pixel), not stocked by big retailers
3. For social and marketplace sellers, one extra search finds their own website, Facebook and published email.
4. Reads the website (free) for published email, phone, socials, ad pixel, products and location.
5. Keeps US **and** AU sellers in any run, labelled with their own country and timezone.
6. Gemini writes the brand name, summary, angle, opener, a ready DM (US or AU tone), call angle and email subject + first line. It drops established brands and accounts that sell nothing, and labels personal accounts that sell.

**Files:**
- `.env.txt`: keys + sheet link (never share)
- `seen_domains.txt`: everything already checked, so nothing is paid for twice
- `do_not_contact.txt` + the sheet's **Do Not Contact** tab: these are always skipped
- `sheet_webhook.gs` + `SHEETS SETUP.md`: the sheet connection

**Cost per run:**
- Roughly 3–4 Serper searches per qualified lead (2,500 free, then ~$1 per 1,000)
- Gemini on the free tier

**Limits:**
- Follower counts come from Google's snippet and can be out of date.
- "Running ads = Y" means a pixel is installed, not proof of spend.
- Country "US?" means it wasn't confirmed; check before calling.
- Review "Personal account" rows before DMing.

# Agency Finder (partnership leads)
```
python agency_finder.py --count 50                 (US + AU, owner-run agencies on IG/TikTok)
python agency_cleanup.py --dry-run                 (finds IG handles for TikTok-only rows; preview)
python agency_cleanup.py --drop ig:handle tt:x     (apply, and delete rows by Key)
```
- Rows go to the sheet's **Agencies** tab, saved after each batch (append only).
- Skips anyone on that tab, in `seen_agencies.txt` or in Do Not Contact.
- Searches are cached in `output/agency_serper_cache.json`, so a rerun never pays twice.
- Gemini's free daily quota on the main models can run out; it then falls back to the lite models automatically.
