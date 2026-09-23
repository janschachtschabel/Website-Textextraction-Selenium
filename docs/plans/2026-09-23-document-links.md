# The extraction keeps the files a page's content links to

## Goal

Work through what the 3.2.0 source run
([2026-09-23-auto-js-fallback.md](2026-09-23-auto-js-fallback.md)) left open, and change code only
where a cause is shown: kindOERgarten lost words when auto rendered it, a KMap exercise was taken
for text hidden in shadow roots, and LEIFI's challenge takes about 12 s to answer "blocked" for
the crawler user agent. The defaults stay as they are.

## Evidence

- **kindOERgarten.** The rendered page still holds the worksheet list: a `<ul>` of two PDF links
  in `.entry-content`, as served. Trafilatura drops that list from both documents - a list of
  nothing but links reads as navigation to it; `favor_recall` does not change that. Over HTTP its
  main extraction found too little, and its fallback happened to keep the worksheet names as
  plain text, without their links. Rendered, the main extraction succeeded with the related posts
  instead: 57 words, no worksheet.
- **The same loss elsewhere.** Over HTTP, 8 of the run's 94 pages link files outside navigation,
  header, footer and sidebar; 6 of them lost at least one of those links: both kindOERgarten
  posts (the worksheets), both Planet-N modules (the module's PDF, linked as "hier") and two
  Science in School articles (the article as PDF, a worksheet). Only Ginkgomaps kept its maps.
- **Choosing the answer with more words** would not help auto. Of the 28 pages it rendered, three
  gave fewer words than over HTTP: kindOERgarten (70 to 57), memucho (86 to 80 - the six words
  were "Log in to see your wikis") and a PhET Java applet (2 to 1). The count does not tell the
  better answer apart.
- **The KMap exercise** (`/app/exercise/...`) shows "lala" under its header after 15 s in
  Chrome; its shadow roots hold the navigation only, and it requests nothing but the login state
  and the subject list. There is nothing to extract, in shadow roots or elsewhere.
- **BR and Fobizz.** Both BR podcast episodes redirect to the series page at ARD Sounds, which
  lists current episodes, not the linked one; both Fobizz materials redirect to the gallery.
  Dead links, like the 404s.
- **LEIFI's challenge.** With the crawler user agent, Chrome shows "Performing security
  verification" unchanged for 14 s. With a browser user agent the page is through by the time
  `driver.get` returns, after 2.1 s.

## Decisions

- After Trafilatura's text, the converter adds the files the content links to that the text
  lacks: a thematic break, then one `- [label](url)` line per file, in page order.
- A file is an http or https link whose path ends in one of `DOWNLOAD_EXTS`, the extensions for
  which `links` reports `download`.
- The content is what browsers do not map to the page's landmarks: not inside `nav`, a
  `header` or `footer` outside `article`, `aside`, `main`, `nav` and `section`, an `aside`
  outside `article` and `section`, or the roles navigation, banner, contentinfo and
  complementary. An article's own header, footer or aside is content.
- Hidden links do not count: the `hidden` attribute other than `until-found`,
  `aria-hidden="true"`, an inline `display: none` or `visibility: hidden`. Wikipedia keeps its
  archive placeholders, `http://IABotmemento.invalid/...`, in hidden spans; without that rule
  "Albanien" gained two.
- A link without text is labelled with its aria-label, its title or its file name.
- Only Trafilatura's clean output changes: markitdown keeps links itself, bs4 and the raw text
  carry none.
- The listed files count as extracted text, so also toward auto's 500 visible characters: a page
  that lists its files is not an empty shell. On the run's pages no page changed between HTTP
  and Chrome.
- `CACHE_VERSION` becomes `extraction-v6`, so stored answers without their files are fetched
  again.

## Out of scope

- A rule that picks the HTTP or the rendered answer by length; see the evidence.
- A shorter `CHALLENGE_LIMIT_SECONDS`. It would save about 4 s per refused page, but challenges
  that take about 5 s by design - DDoS-Guard, Cloudflare's "I'm Under Attack" mode - would be
  cut off on a slower server. So would a memory of hosts whose challenge failed, which trades a
  new state for time on refused pages. Asking the sources to let the crawler in removes the
  wait.
- Reading shadow roots into the extraction: no page of the run needed it.
- Page chrome marked only by an id or class, such as `<div id="footer">`: its files count. The
  rule reads elements and roles, as browsers do, not names; the run had no such page.
- A limit on the list. A page that links thousands of files in its content keeps them all; the
  review measured 2.4 s on top of Trafilatura's 2.7 s for a list of 20,000.

## Tests first

- `tests/test_conversion_results.py`: a worksheet post - three PDF links in its content, one of
  them without text, files linked from navigation, sidebar and footer, two hidden ones - yields
  exactly the three content files after a `---` line; red before the change, and red for the
  hidden ones until hidden elements counted as outside the content. An article whose text keeps
  its file link neither repeats it nor gains a block.
- After the review: `document_links` counts a file by where it sits (20 cases - landmarks, an
  article's own header, footer and aside, hidden and `until-found`), labels a link without text
  by aria-label, title or file name, and recognises files as `links` recognises downloads; each
  red before the change.

## Found in review: one malformed link failed the whole page

A link no URL parser accepts - a template's `http://[URL]`, a tutorial's `http://[Server-IP]/` -
made `urljoin` raise while the converter made the page's references absolute. The conversion
failed and the service answered 502 for the whole page; `extract_links` and KMap attachments
raised the same way. That was so in 3.2.0 already.

- `links.absolute_url` returns None for such a reference. The converter drops it and keeps the
  link's text, a malformed `<base>` falls back to the page's URL, `links` and attachments skip it.
- Tests first: the converter with such a link and with such a `<base>`, a KMap attachment and
  the `links` field; all four raised `ValueError` before the change. The worker path the service
  runs per page - conversion, links, metadata, routing - raised on 3.2.0 and converts now.

## Results

The run's 94 pages again, default settings, the published 3.2.0 image with the branch's code
mounted, against the 3.2.0 run - once before the review round and once with the final code, with
the same result:

- The six pages keep their files: both kindOERgarten posts (over HTTP 70 to 87 and 71 to 76
  words; rendered by auto 57 to 74 and 71 to 83), both Planet-N modules and the two Science in
  School articles. No other page gained a block; Wikipedia's archive placeholders stay out.
- auto answers 64 pages usefully instead of 63 - kindOERgarten passes 60 words - and renders the
  same 28 pages.
- The six pages gained their block on both paths, over HTTP and rendered by Chrome; the list
  shows exactly the files named above.
- The other differences come from the sites: GeoGebra shows other related materials on every
  request. In the first run a Schulcampus RLP page gave its error text translated, and PhET's
  list took 42 s once; three runs each on 3.2.0 and on the branch took 9 to 16 s, with 556 words
  every time.
