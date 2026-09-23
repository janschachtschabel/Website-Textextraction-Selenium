# auto mode renders when the plain HTTP extraction fails

## Goal

`mode=auto` reads a page over HTTP and renders it in Chrome when that extraction fails:
too little text from a page that runs JavaScript. It renders so that content loaded after
the page itself is awaited, and the browser's wait recognizes text that web components keep
in shadow roots.

## Evidence

Measured on 2026-09-23 against the published 3.1.0 image with default settings. Words of
the answer; "extracted" is `visible_length` of the HTTP extraction, the figure the new rule
reads.

| Page | extracted | auto | js speed | js accuracy |
|---|---|---|---|---|
| edu-sharing search (Angular, `<es-app>`) | 0 | 1 (loader image) | 42 | 42 |
| Duolingo (`#root`, text is the title) | 8 | 1 | 170 | 170 |
| Excalidraw | 10 | 1 | 74 | 74 |
| PhET simulations | 55 | 10 | 24 | 419 |
| x.com | 96 | 21 | 61 | 38 |
| Khan Academy (inline loader, no `script[src]`) | 167 | 35 | 2575 | 2575 |
| diagrams.net | 297 | 62 | 62 | 1983 |
| Serlo /mathe | 418 | 74 | 74 | 74 |
| LearningApps | 571 | 77 | 64 | 64 |
| KMap lesson (web components, embedded lesson) | 761 | 129 | 129, not settled | 129, not settled |
| WLO subject portal, GeoGebra, ARD, ZDF, angular.dev, tagesschau | >= 1031 | as js or more | | |
| Leifi (Cloudflare challenge, 403, default user agent) | 11 | 3, blocked | 3, blocked | 26, blocked |

Every thin auto answer above reported `success: true`, so it was cached for
`RESULT_CACHE_TTL` and, with validators, revalidated for up to `REVALIDATION_TTL`.

Causes, each confirmed on these pages:

1. **Routing** (`app/preflight.py`): a thin page renders only if the extraction produced
   nothing or the whole document text is literally a loading phrase, the page has a
   `script[src]`, and one of `#root, #app, #__next, [ng-version]`. Real shells always carry
   some text, at least the `<title>`, which the last converter fallback returns as an `ok`
   extraction. Of nine typical framework shells run through the real converter, only one
   without a title was routed.
2. **Strategy**: auto renders with `DEFAULT_JS_STRATEGY`, `speed` by default. Its wait ends
   after 0.3 s of unchanged text from `DOMContentLoaded` on; a page that shows its header
   first and loads its content afterwards is read too early. Lifting speed's file block
   changes nothing (PhET 25, diagrams.net 62 words); waiting longer does (PhET 419 after
   3 s but 25 after 1.5 s, diagrams.net 1983 after 1.5 s).
3. **Shadow DOM**: the wait reads `innerText` of the light DOM. KMap renders into the shadow
   root of `<kmap-main>`, so the wait sees no text, gives up after its 10 or 20 s limit and
   the answer is not a success although it holds the lesson. The shadow root holds 2658
   characters 0.8 s after the page loaded, unchanged from then on.
4. **Challenge pages**: leifiphysik.de answered plain HTTP with a Cloudflare challenge (403,
   "Just a moment..."), whichever user agent was sent. auto never renders a 4xx page, and
   Chrome reads a 4xx page at once, without waiting. What the challenge does next depends
   on Cloudflare's decision:
   - With the service's default user agent, which names it as a crawler, Chrome stayed on
     the challenge for 25 s.
   - With a browser user agent, as the user's local `.env` sets, `accuracy` got the article
     (200, 7405 characters): its full page load took 3.9 s, long enough for the challenge
     to finish. The user saw the same, `speed` staying on the challenge.
   - In another run with the browser user agent, Cloudflare did not challenge at all.

## Decisions

Taken by the user on 2026-09-23:

1. auto falls back to Chrome when the simple HTTP extraction fails.
2. auto renders with `accuracy` unless the request names `js_strategy`; `mode=js` keeps
   `DEFAULT_JS_STRATEGY`.

Engineering decisions, argued here:

- **The rule reads the extraction, not the markup.** Render when the HTTP extraction has
  fewer than 500 visible characters (`THIN_TEXT_LIMIT`) and the page runs JavaScript: a
  `<script>` with `src`, or an inline one of a JavaScript type (none, `module`, or a
  `*javascript*`/`*ecmascript*` MIME type; JSON-LD and other data blocks do not count).
  - 500 sits between the pages that gain from rendering (at most 297) and those that lose
    (LearningApps 571, KMap 761); Serlo's landing page (418) renders for the same answer.
  - Body text instead of the extraction would render KMap, whose body is empty but whose
    embedded lesson the HTTP path already reads; that answer would lose `success` and take
    12 to 23 s.
  - A list of app roots misses custom elements (`<es-app>`, `<app-root>`) and inline
    loaders; the JavaScript-phrase rule adds nothing once the extraction is thin.
- **A challenge page renders; other errors do not.** The challenge phrases `blocked_content`
  already knows mark it, whatever its status; Cloudflare's page runs JavaScript. A 404, a
  429 or a 5xx stays on HTTP: rendering would not change the answer, and a 429 asks for
  less traffic, not more.
- **Chrome waits while a challenge shows**, with either strategy: while the title names a
  check a browser can pass ("just a moment", "checking your browser", "verifying you are
  human"), it polls for up to 10 s (`CHALLENGE_LIMIT_SECONDS`), then reads the status again
  and waits for content as usual; an expired deadline ends it with 504 like every other
  wait. Cloudflare's "Attention Required!" block page counts as blocked but is neither
  rendered nor awaited: it never lets a browser through. Without the wait, `speed` returns
  the challenge that a moment later lets the page through, and `accuracy` does whenever the
  challenge finishes after the load event. `js_auto_wait=false` switches it off along with
  the other waits. A fake-driver test covers a challenge that never clears; the Chrome test
  covers one that does.
- **The wait falls back to open shadow roots** when the light DOM has no text, skipping
  `style`, `script`, `template` and `link` children. Pages with light-DOM text wait exactly
  as before.
- **`CACHE_VERSION` becomes `extraction-v5`**: stored thin answers would otherwise be served
  again, for up to a day through revalidation.
- **Release 3.2.0**, not a patch: auto's default strategy changes and it renders more pages.

## Second package: data requests, and more sources

Decided by the user on 2026-09-23 after the first end-to-end run: wait for running data
requests before the release, test more of the large OER sources, then release 3.2.0.

- **Evidence.** PhET's list arrived 2.2 s after its page load, together with the last of the
  five XHR requests that were running at that moment. The wait for 0.3 or 1 s of unchanged
  text can end in between: in one run all modes got 25 words, in three earlier ones
  `accuracy` got 419.
- **Decision.** Content counts as settled only while no XHR, fetch or script request of the
  page is running and none started or ended within the stability window - for at most
  `REQUEST_WAIT_SECONDS` (5 s) after the wait began. Scripts count because diagrams.net builds
  its app from scripts it loads after the document: with requests for data only, `speed` still
  read its 62-word landing text; with scripts, the app's 1983 words (ZDF took 7.4 s instead of
  4.5 s for the same text). After that, text stability alone decides
  again, so a page that polls or keeps a connection open settles as before, just later.
  The wait reads Chrome's performance log as it goes and hands every entry back, since the
  main document's status is read from the same log afterwards.
- **Tests first.** Fake-driver tests: the wait lasts until the request the content depends
  on finished; a request that never ends holds it for `REQUEST_WAIT_SECONDS` only; the log
  read during the wait is handed back; each for XHR, fetch and script. Two Chrome fixture
  pages load their content from a slow endpoint, one with fetch and one with a script, and
  must be read with it, with `speed`.
- **More sources.** Two real material pages each for about 50 of the largest sources in
  WLO's list, taken from the WLO index, through fast, auto and js; failures sorted by
  cause, fixes only where a cause is shown, then the same run again.

## Results of the source run

Two material pages each from 46 of WLO's largest sources, taken from the WLO index, plus the
user's three pages and two controls - 94 pages, default settings, the published image with
the branch's code mounted. "Usable" means a success with at least 60 words.

- **auto**: 63 usable answers instead of 53 on 3.1.0. Rescued: OERSI (twillo, doi.org), memucho,
  the WLO and Schulcampus RLP edu-sharing render pages, PhET's lists, Siemens Stiftung, Walter
  Fendt's apps, YouTube. One page lost words: kindOERgarten, 57 instead of 70.
- **What the run found and fixed**, each test first: Fobizz failed on every page with 502,
  because the egress guard named port 80 in Host and the site redirected to
  https://app.fobizz.com:80/; Siemens Stiftung failed with 502 on "Content-Encoding: (with ";
  YouTube, the second largest source, gave its footer over HTTP and Google's consent page in
  Chrome - its title, channel and description now come from the data the watch page embeds,
  and auto keeps a page that carries its content as data on HTTP.
- **The 31 answers that stay weak**, by cause: 11 pages with little text of their own (podcast
  and video pages, apps and simulations, a KMap exercise built in shadow roots); 9 dead links in
  WLO's index (404s, a deleted domain, two Fobizz materials redirected to the gallery, two
  RPI-Virtuell pages whose firewall answered the crawler with 403 and a browser with 404 - live
  RPI-Virtuell pages answer the crawler); 7 refused the service's crawler user agent and served
  a browser one (DiLerTube's 423, OER Commons' 403, LEIFI's Cloudflare challenge, which Chrome
  passes with a browser user agent); 1 Globales Lernen page refused both; 2 Digital Learning Lab
  pages did not answer on port 443 from the test network; 1 video file, skipped by the media
  policy. With a browser user agent auto answered 69 pages usefully instead of 63, but
  kindoergarten.wordpress.com refused the stale `Chrome/127` string with 403.
- **Time**: fast median 1.0 s; auto median 0.9 s where HTTP sufficed (63 pages) and 9.3 s where
  it rendered (28 pages, 90 % within 15.2 s).

- **speed and accuracy** on the 49 pages where JavaScript mattered, plus five plain ones: the
  same text on 46; the other three were a 404 page and two kindOERgarten pages on which `speed`
  failed. Median 5.2 s for `speed`, 6.0 s for `accuracy`; `accuracy` took 1.17 times as long
  where both succeeded.
- **The kindOERgarten failure**: `speed` blocked image requests by URL, the page's blocked
  requests failed, and its tab crashed a few seconds after load - 3.1.0 read the page before
  that, and crashed the same way once told to wait five seconds. `speed` now switches images
  off in Chrome instead; both pages answer with `speed`, PhET and diagrams.net unchanged.

## Out of scope

- Disguising the browser as a person (hiding `navigator.webdriver`, faking plugins), which
  the version before the 2026-09 rebuild did. That circumvents a site's deliberate bot
  protection. Waiting for a challenge is what any browser does; whether the site then
  lets the browser in stays the site's decision.
- The default user agent. It names the service as a crawler with a contact URL; an operator
  who sets a browser user agent decides that for their deployment.
- Reading content that exists only in shadow roots into the extraction (serializing them
  with `getHTML`); KMap's lesson reaches the extraction through its embedded payload.
- Network-idle readiness for `speed`; auto no longer uses it by default.
- `success: true` for a fast-mode answer that is only the page title.

## Tasks, test first

1. `tests/test_network.py`, red first: realistic shells route - a title and `#root`, a
   loader image in `<es-app>`, a custom element with a module script, an inline loader
   without `script[src]`, a 403 challenge page with its inline script; a KMap-like page
   with an empty body but a 600-character extraction does not; a thin page without
   JavaScript or with only JSON-LD does not; a 404, 429 or 503 page without a challenge
   phrase does not. Existing routing tests stay unchanged. Then `app/preflight.py`. Green.
2. `tests/test_api.py` or the options tests, red first: auto resolves to `accuracy` when
   the request names no strategy, keeps a named one, and `mode=js` keeps
   `DEFAULT_JS_STRATEGY`. Then `app/schemas.py` (`resolve_options`). Green.
3. `tests/test_selenium_integration.py`, red first in Chrome (run inside the image, with
   `RUN_SELENIUM_TESTS=1`): a page whose text sits in an open shadow root settles before
   the auto-wait limit. Then the snapshot in `app/browser_readiness.py`. Green.
4. `tests/test_selenium_integration.py`, red first in Chrome: a fixture challenge (403,
   title "Just a moment...", a script that sets a cookie and reloads after about a second,
   then 200 with content) yields the content with `speed`; a challenge that never clears
   ends at the auto-wait limit as today. Then `app/js_fetcher.py`. Green.
5. `app/result_cache.py`: `extraction-v5`.
6. Documents: the /docs texts of `mode`, `js_strategy` and `fetch_engine` (`app/schemas.py`,
   `app/main.py`), README, `docs/settings.md` (`DEFAULT_MODE`, `DEFAULT_JS_STRATEGY`),
   CHANGELOG.
7. Proof on an image built from the branch: the pages above through fast, auto, js speed
   and js accuracy, against the 3.1.0 table.

## Risks

| Risk | Mitigation |
|---|---|
| More auto requests render, each taking seconds and a browser slot | only pages under 500 visible characters that run JavaScript; measured on 18 real pages |
| A rendered answer is worse than the HTTP one | not seen on any routed page; the threshold keeps LearningApps and KMap on HTTP |
| Walking shadow roots on every poll costs time | only while the light DOM shows no text, which pages with text never reach |
