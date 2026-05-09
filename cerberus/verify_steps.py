"""Reproduce/verify steps for each check, embedded for the dev-team report.

Source: the "How to Verify" and "Pass Condition" columns of the SEO/GEO Pre-Flight Checklist
sheet (the brief's source of truth). Copying these here lets the report render concrete
copy-and-paste reproduction commands per failed item.

Substitution placeholders (replaced at render time):
  <url>      — the audited URL
  <host>     — hostname (e.g. www.preprod.strikingly.com)
  <scheme>   — http or https
  <path>     — URL path
  <robots>   — full robots.txt URL
"""
from __future__ import annotations

from urllib.parse import urlparse

# Each entry: (verify_steps, pass_conditions). Multi-line strings with literal placeholders.
STEPS: dict[str, tuple[str, str]] = {

    "A1": (
        "1. curl -s <url> > page.html (raw HTML, pre-JS)\n"
        "2. Render the page in a headless browser (Playwright/Puppeteer) with JS enabled; extract\n"
        "   primary content text: document.querySelector('main, article').innerText → rendered.txt\n"
        "3. For each non-empty line in rendered.txt, grep -F \"<line>\" page.html",
        "1. page.html returns 200 with non-empty HTML body\n"
        "2. rendered.txt is non-empty (the rendered page has visible content in <main> or <article>)\n"
        "3. Every non-empty line in rendered.txt appears verbatim in page.html (confirms SSR — all\n"
        "   visible text is present in raw HTML before JS executes)",
    ),

    "A2": (
        "1. Open the page in Chrome DevTools Performance panel; enable Screenshots; set Network\n"
        "   throttling to Slow 3G.\n"
        "2. Record a fresh page load in incognito.\n"
        "3. Step through frame-by-frame screenshots and compare the first text-bearing frame to the final frame.",
        "1. The first frame showing text content displays the same primary visible content as the final frame.\n"
        "2. No text nodes are replaced between the first text-bearing frame and the load event.",
    ),

    "A3": (
        "curl -o /dev/null -s -w '%{http_code}\\n' <url>",
        "Returns 200 for live indexable pages (NOT 301 to homepage; that's a soft-404). "
        "Removed/nonexistent URLs are F5's territory, not A3's.",
    ),

    "A4": (
        "1. Inspect the URL for a # fragment.\n"
        "2. curl -s <url> | grep -iE 'window\\.location\\.hash|router.*hash|#!' (find hash-routing evidence)\n"
        "3. Fetch <url> with the fragment stripped and compare to the fragmented version.",
        "1. URL works without any # fragment.\n"
        "2. grep returns no matches for hash-based routing patterns.\n"
        "3. Page HTML is byte-identical with or without fragment.",
    ),

    # --- B section ---
    "B1": (
        "curl -s <url> | grep -oE '<title>[^<]*</title>'",
        "Exactly one <title> element in <head> with non-empty text content.",
    ),
    "B2": (
        "curl -s <url> | grep -oE '<meta name=\"description\"[^>]*>'",
        "Exactly one <meta name=\"description\"> element in <head> with non-empty content attribute.",
    ),
    "B3": (
        "1. curl -s <url> | grep -oE '<link rel=\"canonical\"[^>]*>'\n"
        "2. Extract the href value.\n"
        "3. Compare href to <url> (normalize: lowercase, strip trailing slash, strip query params).",
        "1. Exactly one <link rel=\"canonical\"> element in <head>.\n"
        "2. href is a fully qualified absolute URL.\n"
        "3. href matches <url> (modulo normalization) — page self-canonicalizes.",
    ),
    "B4": (
        "1. Extract canonical href from the page.\n"
        "2. curl -sI <canonical-url>\n"
        "3. curl -s <canonical-url> | grep -iE '<meta name=\"robots\"|x-robots-tag'\n"
        "4. curl -s <canonical-url> | grep -oE '<link rel=\"canonical\"[^>]*href=\"[^\"]*\"'",
        "1. Canonical target returns 200.\n"
        "2. Canonical target has no noindex directive.\n"
        "3. Canonical target self-references.",
    ),
    "B5": (
        "1. curl -sI '<url>?utm_source=test'\n"
        "2. curl -sI 'http://<host><path>'\n"
        "3. curl -sI 'https://<apex><path>'   # apex variant\n"
        "4. curl -s <url> | grep 'rel=\"canonical\"'\n"
        "5. curl -s <sitemap-url> | grep -F '<loc><url></loc>'",
        "1. UTM variant returns 200 with same rendered content as canonical.\n"
        "2. http://www variant 301s to https://www<path>.\n"
        "3. apex variant 301s to https://www<path>.\n"
        "4. Canonical tag value = <url> (no trailing slash, no query params).\n"
        "5. Sitemap contains exactly one entry: <url>.",
    ),
    "B6": (
        "1. curl -sI 'http://<apex><path>'\n"
        "2. curl -sI 'https://<apex><path>'",
        "Both return 301 with Location = https://<host><path>.",
    ),
    "B7": (
        "1. curl -sI <url> | grep -i 'x-robots-tag'\n"
        "2. curl -s <url> | grep -iE '<meta name=\"robots\"|<meta name=\"googlebot\"'",
        "1. No X-Robots-Tag response header containing noindex.\n"
        "2. No <meta name=\"robots\"> or <meta name=\"googlebot\"> tag with content containing noindex.",
    ),
    "B8": (
        "1. curl -sI <url> | grep -i 'x-robots-tag'\n"
        "2. curl -s <url> | grep -iE 'name=\"robots\"|name=\"googlebot\"'\n"
        "3. curl -s <url> | grep -i 'data-nosnippet'",
        "1. No X-Robots-Tag header containing nosnippet, max-snippet:0, max-image-preview:none, or unavailable_after.\n"
        "2. No meta robots/googlebot tag containing those directives.\n"
        "3. No data-nosnippet attribute on any element.",
    ),
    "B9": (
        "curl -s <url> | grep -oE '<html[^>]*lang=\"[^\"]*\"'",
        "<html> element has a non-empty lang attribute matching the page language.",
    ),
    "B10": (
        "curl -s <url> | head -c 2048 | grep -iE '<meta charset='",
        "<meta charset=\"UTF-8\"> present in <head> within the first 1024 bytes.",
    ),
    "B11": (
        "curl -s <url> | grep -oE '<meta name=\"viewport\"[^>]*>'",
        "<meta name=\"viewport\"> present in <head> with content including width=device-width.",
    ),
    "B12": (
        "1. curl -s <url> | grep -E 'property=\"og:(title|description|type|url|image|site_name|locale)\"'\n"
        "2. curl -s <url> | grep -E 'name=\"twitter:(card|site|title|description|image)\"'",
        "1. All 7 OG tags present + non-empty: og:title, og:description, og:type, og:url, og:image, og:site_name, og:locale.\n"
        "2. All 5 Twitter Card tags present + non-empty: twitter:card, twitter:site, twitter:title, twitter:description, twitter:image.",
    ),
    "B13": (
        "1. Inspect canonical URL for uppercase characters.\n"
        "2. curl -sI 'https://<host>/<PATH-WITH-CAPS>'\n"
        "3. curl -s <sitemap-url> | grep -E '[A-Z]'\n"
        "4. curl -s <url> | grep -oE '<a [^>]*href=\"[^\"]*\"' | grep -E 'href=\"[^\"]*[A-Z][^\"]*\"'",
        "1. Canonical URL path contains only lowercase letters, digits, and hyphens.\n"
        "2. Uppercase variant returns 301 to lowercase canonical, or 404.\n"
        "3. No sitemap entries contain uppercase letters in path.\n"
        "4. No internal <a href> points to a mixed-case URL.",
    ),
    "B14": (
        "1. curl -s <url> | grep 'property=\"og:image\"' (extract URL)\n"
        "2. curl -sI <og:image-url>\n"
        "3. Download og:image and inspect dimensions (Pillow / ImageMagick identify / file header).\n"
        "4. Repeat 1–3 for twitter:image.",
        "1. og:image URL begins with https://.\n"
        "2. Returns 200 with Content-Type: image/jpeg | image/png | image/webp; size < 5MB.\n"
        "3. Dimensions ≥ 1200×630.\n"
        "4. twitter:image passes the same conditions.",
    ),
    "B15": (
        "1. curl -sI 'https://<host>/favicon.ico'\n"
        "2. curl -s <url> | grep -iE 'rel=\"(icon|shortcut icon|apple-touch-icon)\"'\n"
        "3. For each href from step 2, curl -sI <resolved-url>",
        "1. /favicon.ico returns 200 with image content-type.\n"
        "2. Page <head> contains at least one <link rel=\"icon\"> with non-empty href.\n"
        "3. Every referenced favicon URL returns 200 with image content-type.",
    ),

    # --- C section ---
    "C1": (
        "1. curl -s <url> | grep -c '<h1'\n"
        "2. curl -s <url> | grep -oE '<h1[^>]*>[^<]*</h1>'\n"
        "3. curl -s <url> | grep -oE '<h[1-6][^>]*>[^<]*'",
        "1. Exactly one <h1> element.\n"
        "2. <h1> inner text is non-empty.\n"
        "3. Heading levels do not skip arbitrarily (e.g. h1→h4 with no h2/h3 between).",
    ),
    "C4": (
        "1. curl -s <url> | grep -oE '<(input|textarea|select)[^>]*id=\"[^\"]*\"'\n"
        "2. For each id from step 1: curl -s <url> | grep -F 'for=\"<id>\"'\n"
        "3. Open URL in Chrome → DevTools → Lighthouse → Accessibility audit → 'Form elements do not have associated labels'.",
        "1. Every <input>/<textarea>/<select> has an id.\n"
        "2. For each id, a <label for=\"<id>\"> or wrapping <label> exists.\n"
        "3. Lighthouse reports zero form-label errors.",
    ),
    "C5": (
        "1. curl -s <url> | grep -oE '<img[^>]*'\n"
        "2. For each <img>, verify alt attribute is explicitly declared (alt value may be empty).",
        "Every <img> has an alt attribute. No <img> is missing the attribute entirely.",
    ),
    "C6": (
        "1. curl -s <url> | grep -oE '<main|<article'\n"
        "2. curl -s <url> | grep -oE '<nav|<header|<footer|<aside'\n"
        "3. Open URL in Chrome → DevTools Console:\n"
        "   Array.from(document.querySelectorAll('main, article')).map(e => ({tag: e.tagName, insideBoilerplate: !!e.closest('nav, header, footer, aside')}))",
        "1. Primary content lives inside a <main> or <article> element.\n"
        "2. Navigation uses <nav>; boilerplate uses <header>/<footer>; sidebar uses <aside>.\n"
        "3. At least one <main>/<article> has insideBoilerplate: false.",
    ),
    "C7": (
        "1. Load page in fresh incognito window, mobile viewport (375px).\n"
        "2. Load page in fresh incognito window, desktop viewport (1440px).\n"
        "3. On first paint, identify any modal/overlay/newsletter gate/cookie wall/app-install prompt that obscures primary content.",
        "No modal/overlay blocks primary visible content (hero, primary CTA) on first paint, mobile or desktop.\n"
        "Cookie consent banners are acceptable only if limited to legal-minimum GDPR/CCPA.",
    ),

    # --- D section ---
    "D2": (
        "1. View page source → find 'application/ld+json' → copy each <script> block's content.\n"
        "2. Paste each JSON-LD block into Google Rich Results Test (https://search.google.com/test/rich-results).\n"
        "3. Paste each block into Schema.org Validator (https://validator.schema.org).",
        "1. Rich Results Test reports zero errors.\n"
        "2. Schema.org Validator reports zero errors.\n"
        "3. All required fields for the declared @type are present.",
    ),
    "D3": (
        "1. curl -s <url> | grep -A 50 'application/ld+json' > json-ld.txt\n"
        "2. Render page in headless browser; document.body.innerText → rendered.txt\n"
        "3. For each BreadcrumbList itemListElement: grep -F \"<name>\" rendered.txt\n"
        "4. For each URL in JSON-LD: confirm matches canonical or visible <a href>.\n"
        "5. Manually inspect free-text fields for content contradicting the visible page.",
        "1. JSON-LD parses as valid JSON.\n"
        "2. rendered.txt is non-empty.\n"
        "3. Every BreadcrumbList itemListElement name appears verbatim in rendered.txt.\n"
        "4. Every URL field matches canonical or a visible link.\n"
        "5. No free-text field contradicts visible content.",
    ),
    "D5": (
        "N/A until hub architecture exists (per brief). Once it does:\n"
        "1. curl -s <url> | grep -A 30 '\"@type\":\"BreadcrumbList\"'\n"
        "2. Validate the JSON-LD block; for each itemListElement check item URL status and visible breadcrumb match.",
        "BreadcrumbList JSON-LD present with itemListElement entries matching the page's breadcrumb hierarchy.",
    ),

    # --- E section ---
    "E1": (
        "Run PageSpeed Insights (https://pagespeed.web.dev/) against <url>; check mobile and desktop tabs;\n"
        "if page has been live 28+ days, also read CrUX field data.",
        "1. Lab LCP ≤ 2.5s on mobile.\n"
        "2. Lab LCP ≤ 2.5s on desktop.\n"
        "3. If field data exists, p75 LCP ≤ 2.5s.",
    ),
    "E2": (
        "Run PSI against <url>; check mobile + desktop CLS; read CrUX field data if available.",
        "Lab CLS < 0.1 on both mobile and desktop. If field data exists, p75 CLS < 0.1.",
    ),
    "E3": (
        "Run PSI against <url>; check mobile + desktop INP; read CrUX field data if available.",
        "Lab INP < 200ms on both mobile and desktop. If field data exists, p75 INP < 200ms.",
    ),
    # Backwards-compat: rows from before the E3a split (combined Perf+SEO check) still
    # carry check_id="E3a"; keep this entry so historical /report.md renders correctly.
    "E3a": (
        "Run PSI against <url>; check Performance and SEO scores on mobile + desktop.",
        "Performance ≥ 0.90 and SEO ≥ 0.90 on both mobile and desktop.\n"
        "Accessibility and Best Practices ≥ 0.90 where possible (target, not blocking).",
    ),
    "E3a-perf": (
        "Run PSI against <url>; check Performance score on mobile + desktop.",
        "Performance ≥ 0.90 on both mobile and desktop.",
    ),
    "E3a-seo": (
        "Run PSI against <url>; check SEO score on mobile + desktop.",
        "SEO ≥ 0.90 on both mobile and desktop.",
    ),
    "E3b": (
        "Open <url> in Chrome incognito → DevTools → Lighthouse → Accessibility + Best Practices,\n"
        "Mobile, Navigation mode → Analyze. Repeat with Desktop.",
        "Accessibility ≥ 0.90 and Best Practices ≥ 0.90 on both mobile and desktop.",
    ),
    "E4": (
        "1. DevTools Device Mode → 375px viewport.\n"
        "2. Scroll page; tap each form input and CTA.\n"
        "3. Inspect body text font-size via DevTools computed styles.",
        "1. No horizontal scroll on main layout.\n"
        "2. Form inputs and CTA tappable without overlap.\n"
        "3. Body text ≥ 14px; no text < 12px except footnotes/legal.",
    ),
    "E5": (
        "DevTools Device Mode 375px → Lighthouse Accessibility audit → 'Tap targets are sized appropriately'.\n"
        "Or in Console: document.querySelectorAll('button,input,textarea,select,a').forEach(e => console.log(e.tagName, e.getBoundingClientRect()))",
        "1. Lighthouse reports zero tap-target errors.\n"
        "2. Every interactive element ≥ 48×48 px.\n"
        "3. Adjacent interactive elements have ≥ 8 px gap.",
    ),
    "E6": (
        "curl -sA 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15' <url> > mobile.html\n"
        "curl -sA 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' <url> > desktop.html\n"
        "diff <(sed -n '/<main/,/<\\/main>/p' mobile.html) <(sed -n '/<main/,/<\\/main>/p' desktop.html)\n"
        "diff <(grep -E '<title|<meta name=\"description\"|<link rel=\"canonical\"' mobile.html) <(grep -E '<title|<meta name=\"description\"|<link rel=\"canonical\"' desktop.html)",
        "1. <main>/<article> content byte-identical between mobile and desktop.\n"
        "2. <title>, <meta description>, canonical byte-identical.\n"
        "3. robots directives + JSON-LD blocks byte-identical.\n"
        "4. Internal <a href> set byte-identical.",
    ),
    "E7": (
        "1. Load <url> in Chrome; inspect address bar for lock icon.\n"
        "2. DevTools Console → check for 'Mixed Content' warnings.\n"
        "3. curl -s <url> | grep -oE '(src|href)=\"http://[^\"]*\"'",
        "1. <url> loads over https:// with no security warnings.\n"
        "2. Zero Mixed Content warnings.\n"
        "3. No critical resources loaded over http://.",
    ),
    "E8": (
        "curl -sI <url> | grep -i 'strict-transport-security'",
        "Strict-Transport-Security header present with max-age ≥ 15552000 (6 months).\n"
        "includeSubDomains directive preferred but not required.",
    ),

    # --- F section ---
    "F1": (
        "1. curl -s <sitemap-url> | grep -F '<loc><url></loc>'\n"
        "2. curl -s <sitemap-url> | grep -c '<url>'\n"
        "3. (Once hub architecture exists) curl -s <sitemap-url> | grep -F '<loc><hub-url></loc>'",
        "1. Sitemap contains a <loc> entry matching <url> exactly.\n"
        "2. URL appears exactly once.",
    ),
    "F2": (
        "curl -s <robots> | grep -i '^Sitemap:'",
        "robots.txt contains at least one 'Sitemap:' directive pointing to a valid sitemap URL.",
    ),
    "F3": (
        "1. curl -sI <robots>\n"
        "2. curl -s <robots> | grep -iE '^(User-agent|Disallow):'\n"
        "3. For each Disallow under User-agent: * AND each named-bot block, compare path prefix vs <path>.\n"
        "4. curl -s <url> | grep -oE '(src|href)=\"/[^\"]*\"' (list internal resource paths) and apply same prefix comparison.",
        "1. robots.txt returns 200 and is parseable.\n"
        "2. No Disallow rule prefix matches <path>.\n"
        "3. No Disallow rule blocks any static resource path required for rendering.",
    ),
    "F4": (
        "1. curl -sI -A 'Googlebot/2.1 (+http://www.google.com/bot.html)' <url>\n"
        "2. curl -sI -A 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0' <url>\n"
        "3. Compare status codes between bot and user.\n"
        "4. Inspect bot response body for challenge pages, login walls, consent gates.",
        "1. Googlebot returns 200 (not 403, 429, or challenge).\n"
        "2. Body content materially equivalent between bot and user.\n"
        "3. No Cloudflare challenge / captcha / login wall / consent gate blocks Googlebot.",
    ),
    "F5": (
        "1. curl -o /dev/null -s -w '%{http_code}\\n' '<scheme>://<host><pipeline-prefix>cerberus-nonexistent-test'\n"
        "2. curl -s '<scheme>://<host><pipeline-prefix>cerberus-nonexistent-test' | head -c 500",
        "1. Returns 404 or 410 (NOT 200, NOT 301 to homepage).\n"
        "2. Response body is the standard error page, not a forked page template.",
    ),
    "F6": (
        "1. On a parent/hub page (or via crawl): curl -s <hub-url> | grep -oE '<a [^>]*href=\"[^\"]*\"' | grep -F '<url>'\n"
        "2. curl -s <url> | grep -oE '<a [^>]*href=\"[^\"]*\"' | grep -E 'href=\"(https?://[^/]*<host>|/)[^\"]*\"'",
        "1. At least one indexable internal page links TO <url>.\n"
        "2. <url> contains at least one outbound <a href> to another indexable internal page.",
    ),
    "F7": (
        "1. Identify parent/hub/sibling pages linking to <url>.\n"
        "2. curl -s <parent-url> | grep -oE '<a [^>]*href=\"[^\"]*\"'\n"
        "3. Confirm the link uses standard <a href> markup.",
        "Link uses <a href=\"...\"> with absolute or root-relative URL; not button-based, JS-only click handler, or form submission. Present in raw HTML, not JS-injected.",
    ),
    "F8": (
        "1. curl -s <url> | grep -oE '<a [^>]*href=\"[^\"]*\"'\n"
        "2. For each internal href: curl -sI <href> and note status code.\n"
        "3. Check each href for tracking parameters (?utm_*, ?session=, ?sid=).",
        "1. Every internal link has stable href to a valid internal URL.\n"
        "2. Every internal link returns 200 (not 301/302).\n"
        "3. No internal href includes tracking parameters.",
    ),
    "F9": (
        "1. curl -s <robots> > robots.txt\n"
        "2. For each bot in the AI-Crawler reference list: grep -iE \"^User-agent:\\s*<bot>\" robots.txt\n"
        "3. For each allowed bot: confirm no Disallow rule matches <path> within that bot's block.\n"
        "4. (Manual) Inspect Cloudflare/CDN WAF dashboard: confirm no rule blocks/rate-limits allowed bot IP ranges.",
        "1. robots.txt has explicit User-agent rules for each bot we intend to allow or block.\n"
        "2. Allow/block decisions match business intent.\n"
        "3. No accidental Disallow rules block intended bots.\n"
        "4. Firewall/CDN does not rate-limit allowed bot IP ranges.",
    ),
    "F10": (
        "1. curl -sI <sitemap-url>\n"
        "2. curl -s <sitemap-url> | xmllint --noout -",
        "1. Sitemap URL returns 200 with Content-Type: application/xml or text/xml.\n"
        "2. Sitemap parses as valid XML with no errors.",
    ),
    "F11": (
        "1. curl -sI <robots>\n"
        "2. curl -s <robots> | head -50",
        "1. Returns 200 with Content-Type: text/plain.\n"
        "2. File contains at least one valid User-agent directive.",
    ),

    # --- G section ---
    "G1": (
        "1. Inspect locale URL patterns for this page (e.g., <host><path>, ar.<apex><path>).\n"
        "2. curl -sI each locale URL.",
        "1. Each locale version has its own unique crawlable URL.\n"
        "2. Each locale URL returns 200 independently (not 301/302 to another locale).",
    ),
    "G2": (
        "For each locale URL: curl -s <locale-url> | grep -oE '<link rel=\"canonical\"[^>]*href=\"[^\"]*\"'\n"
        "Compare each canonical href to the locale URL that was fetched.",
        "1. Every locale page has exactly one canonical tag with non-empty href.\n"
        "2. Every locale's canonical href = its own URL (no cross-canonicals between locales).",
    ),
    "G3": (
        "1. curl -s <url> | grep -oE '<link rel=\"alternate\"[^>]*hreflang=\"[^\"]*\"[^>]*href=\"[^\"]*\"' > hreflang-main.txt\n"
        "2. For each href in hreflang-main.txt: curl -s <locale-href> and extract hreflang declarations the same way.\n"
        "3. diff <(sort hreflang-main.txt) <(sort hreflang-<locale>.txt) for each locale.\n"
        "4. For each unique href: curl -sI <href> | head -1.",
        "1. hreflang-main.txt non-empty with self-reference + alternate locales.\n"
        "2. Every locale page yields a non-empty hreflang set.\n"
        "3. Every diff is empty — cluster is fully reciprocal.\n"
        "4. Every cluster href returns 200.",
    ),
    "G4": (
        "1. For each locale: curl -s <locale-url> | grep 'hreflang=\"x-default\"'\n"
        "2. Extract the href from the x-default entry.",
        "1. At least one page in the cluster declares hreflang=\"x-default\".\n"
        "2. x-default href points to the English (canonical) version.",
    ),
    "G5": (
        "1. For each locale: curl -sI <locale-url> | grep -iE '^(HTTP|Location)'\n"
        "2. For each locale: curl -s <locale-url> | grep -oE '<link rel=\"canonical\"[^>]*href=\"[^\"]*\"'\n"
        "3. Confirm status is 200 (not 301/302 to another locale) and canonical = self.",
        "1. Each locale URL returns 200 (not redirected to another locale).\n"
        "2. Each locale's canonical points to itself.\n"
        "3. Variants only related via hreflang, never via redirects or cross-canonicals.",
    ),
    "G6": (
        "1. curl -s -A 'Googlebot/2.1' <url>   (baseline)\n"
        "2. curl -s -A 'Googlebot/2.1' -H 'Accept-Language: ar' <url>; diff against baseline\n"
        "3. Test fetch from non-US IP via proxy/VPN; diff against baseline\n"
        "4. curl -s -A 'Googlebot/2.1' --cookie 'locale=ar' <url>; diff against baseline",
        "1. Baseline returns 200 with expected locale content.\n"
        "2. Accept-Language does not switch content for the same URL.\n"
        "3. IP geo does not switch content.\n"
        "4. Cookie does not switch content.",
    ),

    # --- H section ---
    "H1": (
        "curl -sA 'Googlebot/2.1 (+http://www.google.com/bot.html)' <url> > bot.html\n"
        "curl -sA 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0' <url> > user.html\n"
        "diff <(sed -n '/<main/,/<\\/main>/p' bot.html) <(sed -n '/<main/,/<\\/main>/p' user.html)",
        "1. bot.html non-empty with <main>/<article> present (Googlebot not served challenge/captcha).\n"
        "2. user.html non-empty with <main>/<article> present.\n"
        "3. diff is empty, OR diffs limited to non-content variance (timestamps, CSRF, nonces).",
    ),
    "H2": (
        "1. curl -s <url> > raw.html; html2text raw.html > raw-text.txt\n"
        "2. Render page in Playwright; document.body.innerText → rendered.txt\n"
        "3. diff raw-text.txt rendered.txt\n"
        "4. curl -s <url> | grep -iE 'display:\\s*none|visibility:\\s*hidden|text-indent:\\s*-?[0-9]{4,}|color:\\s*#?(fff|ffffff|white)'",
        "1. Every text in raw-text.txt also in rendered.txt (or hidden structural element).\n"
        "2. Every text in rendered.txt also in raw-text.txt (no JS-injected-only text).\n"
        "3. No primary-content text node missing from either file.\n"
        "4. No hidden-content CSS tricks on text nodes within <main>/<article>.",
    ),
    "H3": (
        "1. Render <url> in Playwright with Googlebot WRS UA; save rendered HTML → bot.html.\n"
        "2. Render <url> in Playwright with normal Chrome UA; save rendered HTML → user.html.\n"
        "3. diff <(sed -n '/<main/,/<\\/main>/p' bot.html) <(sed -n '/<main/,/<\\/main>/p' user.html)\n"
        "4. Optional ground truth: paste <url> into GSC URL Inspection → Test Live URL → View Tested Page; visually compare against bot.html.",
        "1. Both renders return HTTP 2xx (no anti-bot challenge).\n"
        "2. Both renders contain <main>/<article> with primary content.\n"
        "3. diff empty (or limited to non-content variance).",
    ),
}


def for_check(check_id: str, url: str) -> tuple[str, str] | None:
    """Return (verify_steps, pass_conditions) with placeholders substituted, or None if no entry."""
    entry = STEPS.get(check_id)
    if not entry:
        return None
    p = urlparse(url)
    host = (p.hostname or "").lower()
    apex = host[4:] if host.startswith("www.") else host
    pipeline_prefix = "/"
    if p.path:
        # Best-effort: take everything up to the last segment.
        parts = p.path.rsplit("/", 1)
        pipeline_prefix = (parts[0] + "/") if parts[0] else "/"

    def sub(text: str) -> str:
        return (text
                .replace("<url>", url)
                .replace("<host>", host)
                .replace("<apex>", apex)
                .replace("<scheme>", p.scheme)
                .replace("<path>", p.path or "/")
                .replace("<robots>", f"{p.scheme}://{host}/robots.txt")
                .replace("<pipeline-prefix>", pipeline_prefix))

    return sub(entry[0]), sub(entry[1])
