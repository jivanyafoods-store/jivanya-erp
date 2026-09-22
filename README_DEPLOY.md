# Shreeju + Jivanya Free ERP Stack

## Architecture
Google Sheet remains the operational storefront/catalog source. Google Apps Script remains the public checkout/message bridge. Supabase remains the ERP database. Render hosts the FastAPI ERP API. Both Shreeju and Jivanya sites can send data to the same ERP using `firm_id`.

## Important
- Replace `YOUR-RENDER-SERVICE` in keepalive.yml with the real Render hostname.
- Render free services may still sleep/restart; keepalive is best-effort, not a 24/7 guarantee.
- Never put a Supabase service-role key in HTML.
- Customer visitor analytics should use pseudonymous analytics identifiers. Do not send phone/email/name to Google Analytics.
- Public customer lead capture to Google Sheet/Supabase should happen only after consent or a user-provided contact action.

## FSSAI
Jivanya Foods: FSSAI Lic. No. 22725884000388.

## SEO
Submit sitemap in Google Search Console and Bing Webmaster Tools. IndexNow can notify Bing-compatible engines of changed URLs, but indexing/ranking is not guaranteed.
