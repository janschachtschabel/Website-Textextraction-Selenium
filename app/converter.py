from __future__ import annotations

import json as _json
import os
import re
import subprocess
import tempfile
import threading
import time as _time
import warnings

from bs4 import BeautifulSoup
from markitdown import MarkItDown
from markitdown._exceptions import FileConversionException

from .config import settings
from .utils import guess_extension

try:
    # Optional: Trafilatura for robust HTML content extraction
    from trafilatura import extract as t_extract  # type: ignore
    from trafilatura import html2txt as t_html2txt
except Exception:  # pragma: no cover - optional dependency guarded
    t_extract = None
    t_html2txt = None
from loguru import logger

# Simple in-process circuit breaker for MarkItDown
_MID_FAILURES: list[float] = []  # timestamps of recent unexpected failures
_MID_DISABLED: bool = False
_MID_WINDOW_SEC = 60.0
_MID_FAIL_THRESHOLD = 5  # disable after 5 unexpected failures within window
_MID_LOCK = threading.Lock()  # protects _MID_FAILURES and _MID_DISABLED

# Suppress noisy RuntimeWarnings from pydub/ffmpeg by default
try:
    warnings.filterwarnings("ignore", category=RuntimeWarning, module=r"pydub\..*")
except Exception:
    pass


def preserve_mathematical_content(text: str) -> str:
    """Normalise Unicode mathematical symbols to their standard code points.

    The previous implementation mapped every character to itself (a no-op) and
    applied overly aggressive regexes that wrapped arbitrary identifiers in
    backticks/bold.  This version simply returns the text unchanged because
    modern converters (trafilatura, markitdown) already preserve Unicode symbols.
    The function is kept so callers need no changes.
    """
    return text


def enhance_table_structure(text: str) -> str:
    """Enhance table structure preservation in markdown.

    Only inserts a header-separator row when the line immediately following
    the first pipe-containing line is NOT already a separator (i.e. does not
    match the pattern `| --- | … |`).  This prevents doubling separators in
    already-valid tables.
    """
    lines = text.split('\n')
    enhanced_lines: list[str] = []
    in_table = False
    _sep_re = re.compile(r'^\|[\s:\-|]+\|\s*$')

    for i, line in enumerate(lines):
        if '|' in line and line.count('|') >= 2:
            if not in_table:
                in_table = True
                enhanced_lines.append(line)
                # Only insert a separator if the next line isn't one already
                next_line = lines[i + 1] if i + 1 < len(lines) else ""
                if not _sep_re.match(next_line):
                    cells = line.split('|')
                    separator = '|' + '|'.join(['---' for _ in range(len(cells) - 1)]) + '|'
                    enhanced_lines.append(separator)
            else:
                enhanced_lines.append(line)
        else:
            if in_table:
                in_table = False
                enhanced_lines.append('')
            enhanced_lines.append(line)

    return '\n'.join(enhanced_lines)


def bytes_to_markdown(
    data: bytes,
    content_type: str | None,
    url: str | None = None,
    *,
    html_converter: str | None = None,
    trafilatura_clean_markdown: bool | None = None,
    media_conversion_policy: str | None = None,
    disable_markitdown: bool | None = None,
) -> str:
    """
    Convert arbitrary bytes to Markdown using markitdown[all] with enhanced mathematical and table preservation.
    Strategy: write to a temp file with an appropriate extension based on MIME type.
    """
    global _MID_DISABLED
    # Thread-safe snapshot of the circuit-breaker flag
    with _MID_LOCK:
        _mid_disabled_now = _MID_DISABLED
    # Resolve effective settings (per-request overrides > global defaults)
    eff_html_conv = (html_converter or settings.html_converter or "trafilatura").strip().lower()
    eff_traf_clean = settings.trafilatura_clean_markdown if trafilatura_clean_markdown is None else bool(trafilatura_clean_markdown)
    eff_media_policy = (media_conversion_policy or settings.media_conversion_policy or "skip").strip().lower()
    eff_disable_mid = bool(disable_markitdown)  # default False unless explicitly True

    ext = guess_extension(content_type)
    # If mislabeled PDF (content-type says PDF but bytes don't start with %PDF), treat as HTML/text
    if (content_type or "").lower().startswith("application/pdf") and data[:4] != b"%PDF":
        # override extension to .html to run HTML/text path
        ext = ".html"
    # Bypass unstable generic octet-stream conversions: return a note instead of invoking MarkItDown
    if (content_type or "").lower().startswith("application/octet-stream"):
        link_line = f"\nSource: {url}" if url else ""
        return (
            f"# Unsupported Binary Content\n\n"
            f"Received 'application/octet-stream'. Binary/zip-like payloads are not converted automatically."
            f"{link_line}\n"
        )
    # If we have HTML but content looks binary or empty, still attempt
    fd, path = tempfile.mkstemp(suffix=ext)
    try:

        # Media policy: handle audio/video early to avoid heavy conversions
        is_media = False
        ctype_lower = (content_type or "").lower()
        if ctype_lower.startswith("video/") or ctype_lower.startswith("audio/"):
            is_media = True

        # Write original or pre-cleaned data to temp file
        # Optional pre-cleaning for HTML to remove noscript/JS-required banners
        to_write = data
        if ext == ".html":
            try:
                html_text = data.decode("utf-8", errors="ignore")
                soup = BeautifulSoup(html_text, "lxml")
                # Remove all <noscript> blocks
                for tag in soup.find_all("noscript"):
                    tag.decompose()
                # Remove common JS-required banners by id/class hints
                hints = ["noscript", "no-js", "js-disabled", "enable-js", "javascript"]
                for el in soup.find_all(True):
                    attr_text = " ".join(
                        [str(el.get("id", "")), " ".join(el.get("class", []))]
                    ).lower()
                    if any(h in attr_text for h in hints):
                        # If the element is short text or clearly a banner, drop it
                        txt = (el.get_text(strip=True) or "")
                        if len(txt) <= 200:
                            el.decompose()
                # Remove short texts that explicitly ask to enable JS (DE/EN)
                js_msgs = re.compile(
                    r"(enable\s+javascript|javascript\s+required|please\s+enable\s+javascript|"
                    r"bitte.*javascript.*(aktivieren|einschalten)|javascript\s+wird\s+ben(ö|o)tigt)",
                    re.IGNORECASE,
                )
                for t in soup.find_all(string=js_msgs):
                    parent = t.parent
                    # Only remove if small and likely a banner
                    if parent and len(parent.get_text(strip=True) or "") <= 200:
                        parent.decompose()
                # KMap special-case: extract embedded JSON payload if present
                try:
                    kmap_md = _extract_kmap_markdown(soup, url)
                except Exception:
                    kmap_md = None
                # Universal policy: if embedded JSON yields only a tiny fragment, prefer full-DOM MarkItDown
                if isinstance(kmap_md, str) and kmap_md.strip():
                    if len(kmap_md) >= 800:
                        # Close the unused fd before early-returning to avoid a
                        # file descriptor leak (os.fdopen was not reached yet).
                        try:
                            os.close(fd)
                        except Exception:
                            pass
                        try:
                            os.remove(path)
                        except Exception:
                            pass
                        return kmap_md
                    else:
                        # Force full-HTML MarkItDown conversion to emulate legacy behavior
                        eff_html_conv = "markitdown"
                cleaned = str(soup)
                to_write = cleaned.encode("utf-8", errors="ignore")

                # Prefer Trafilatura for HTML if configured (per-request aware)
                try:
                    if eff_html_conv == "trafilatura" and (t_extract or t_html2txt):
                        extracted = None
                        if eff_traf_clean and t_extract:
                            try:
                                extracted = t_extract(cleaned, base_url=url, output_format='markdown')  # type: ignore[arg-type]
                            except Exception:
                                extracted = None
                            # Retry with broader settings when first pass returns nothing
                            if not (isinstance(extracted, str) and extracted.strip()):
                                try:
                                    extracted = t_extract(  # type: ignore[arg-type]
                                        cleaned, base_url=url, output_format='markdown',
                                        include_comments=True, favor_recall=True,
                                    )
                                except Exception:
                                    extracted = None
                        elif t_html2txt:
                            try:
                                # Raw extraction (not cleaned) – plain text
                                extracted = t_html2txt(cleaned, base_url=url)  # type: ignore[arg-type]
                            except Exception:
                                extracted = None

                        if isinstance(extracted, str) and extracted.strip():
                            text = extracted
                            # Apply math/table enhancements as final polish
                            text = preserve_mathematical_content(text)
                            text = enhance_table_structure(text)
                            return text
                except Exception:
                    # If Trafilatura path fails for any reason, fall through to MarkItDown/BS4
                    pass

                # If explicitly configured to use BS4 for HTML, short-circuit here
                try:
                    if eff_html_conv == "bs4":
                        return _fallback_content_extraction(to_write, content_type or "text/html", ".html")
                except Exception:
                    pass
            except Exception:
                to_write = data

        with os.fdopen(fd, "wb") as f:
            f.write(to_write)

        # Media conversion policy branching (supports: skip|metadata|full|none)
        if is_media:
            policy = (eff_media_policy or "skip").strip().lower()
            if policy not in {"skip", "metadata", "full", "none"}:
                policy = "skip"

            if policy == "skip":
                # Return a lightweight placeholder with link and content type
                link_line = f"\nSource: {url}" if url else ""
                return (
                    f"# Media Resource Skipped\n\n"
                    f"This is a {content_type or 'media'} resource. Conversion is disabled (MEDIA_CONVERSION_POLICY=skip)."
                    f"{link_line}\n"
                )

            if policy == "none":
                # Return truly minimal output to indicate no conversion at all
                return ""

            if policy == "metadata":
                meta = _probe_media_metadata(path)
                meta_text = _json.dumps(meta, ensure_ascii=False, indent=2) if meta else "{\n  \"note\": \"No metadata available\"\n}"
                link_line = f"\nSource: {url}" if url else ""
                return (
                    f"# Media Resource Metadata\n\n"
                    f"Detected type: {content_type or 'media'}\n\n"
                    f"Metadata (ffprobe):\n\n```json\n{meta_text}\n```\n"
                    f"{link_line}\n"
                )
            # else: fallthrough to full conversion

        # Try MarkItDown conversion with comprehensive error handling (can be disabled per-request)
        try:
            # Honor global disable flag and local circuit breaker
            if eff_disable_mid or _mid_disabled_now:
                raise RuntimeError("MarkItDown disabled by configuration or circuit breaker")

            md = MarkItDown()
            result = md.convert(path)
            # markitdown returns an object with .text_content
            text = getattr(result, "text_content", None)
            if isinstance(text, str) and text.strip():
                # Apply mathematical content preservation
                text = preserve_mathematical_content(text)
                # Enhance table structure
                text = enhance_table_structure(text)
                return text
            # Fallback to any string representation
            if isinstance(result, str):
                result = preserve_mathematical_content(result)
                result = enhance_table_structure(result)
                return result
        except FileConversionException as e:
            # Known, expected conversion error (e.g., malformed PDF) -> do NOT trip breaker
            msg = str(e)
            if "PDFSyntaxError" in msg:
                logger.warning(f"MarkItDown PDF syntax error for {content_type}: {e}")
            elif "UnicodeDecodeError" in msg:
                logger.warning(f"MarkItDown unicode/zip decode error for {content_type}: {e}")
            else:
                logger.warning(f"MarkItDown conversion failed for {content_type}: {e}")
            # Fall back to manual content extraction based on content type
            return _fallback_content_extraction(data, content_type, ext)
        except Exception as e:
            # Unexpected failure -> record for circuit breaker and fall back
            try:
                now = _time.time()
                with _MID_LOCK:
                    _MID_FAILURES.append(now)
                    # prune window
                    cutoff = now - _MID_WINDOW_SEC
                    while _MID_FAILURES and _MID_FAILURES[0] < cutoff:
                        _MID_FAILURES.pop(0)
                    if len(_MID_FAILURES) >= _MID_FAIL_THRESHOLD:
                        _MID_DISABLED = True
                        logger.error(
                            f"MarkItDown disabled for this process after {len(_MID_FAILURES)} unexpected failures within {_MID_WINDOW_SEC}s"
                        )
            except Exception:
                pass
            logger.error(f"Unexpected error in MarkItDown conversion: {e}")
            return _fallback_content_extraction(data, content_type, ext)
        
        # If we get here, MarkItDown didn't return usable content
        return _fallback_content_extraction(data, content_type, ext)
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


def _probe_media_metadata(path: str) -> dict:
    """Extract media metadata using ffprobe if available. Returns a dict or {}."""
    try:
        # ffprobe outputs JSON metadata
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return _json.loads(proc.stdout)
    except Exception:
        return {}
    return {}


def _fallback_content_extraction(data: bytes, content_type: str | None, ext: str) -> str:
    """Fallback content extraction when MarkItDown fails."""
    try:
        # For HTML content, try BeautifulSoup extraction
        if ext == ".html" or (content_type and "html" in content_type.lower()):
            try:
                html_text = data.decode("utf-8", errors="ignore")
                soup = BeautifulSoup(html_text, "lxml")
                # Remove script and style elements
                for script in soup(["script", "style"]):
                    script.decompose()
                # Get text content
                text = soup.get_text()
                # Clean up whitespace
                lines = (line.strip() for line in text.splitlines())
                chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
                text = '\n'.join(chunk for chunk in chunks if chunk)
                if text.strip():
                    text = preserve_mathematical_content(text)
                    return text
            except Exception as e:
                logger.warning(f"BeautifulSoup fallback failed: {e}")
        
        # For text-based content, try direct decoding
        if ext in [".txt", ".csv", ".json", ".md", ".xml"] or (content_type and any(t in content_type.lower() for t in ["text", "json", "xml"])):
            try:
                decoded = data.decode("utf-8", errors="ignore")
                if decoded.strip():
                    decoded = preserve_mathematical_content(decoded)
                    return decoded
            except Exception as e:
                logger.warning(f"Text decoding fallback failed: {e}")
        
        # For binary files that failed conversion, return a descriptive message
        if content_type:
            return f"# Content Extraction Failed\n\nUnable to extract text content from {content_type} file.\n\nThe file may be corrupted, password-protected, or in an unsupported format."
        else:
            return f"# Content Extraction Failed\n\nUnable to extract text content from file with extension {ext}.\n\nThe file may be corrupted, password-protected, or in an unsupported format."
    
    except Exception as e:
        logger.error(f"All fallback extraction methods failed: {e}")
        return "# Content Extraction Failed\n\nAll content extraction methods failed. The file may be corrupted or in an unsupported format."


def _extract_kmap_markdown(soup: BeautifulSoup, base_url: str | None) -> str | None:
    """Extract rich content from embedded JSON payloads (universal, not site-specific).

    Strategy:
    - Prefer <script id="embedded-topic" type="json"> when present (historic KMap format)
    - Otherwise scan all <script> tags with type in {json, application/json, application/ld+json}
    - Look for useful fields: description (HTML), attachments (list with href/file), title/headline/name/topic/chapter
    - Rewrite inline: references via attachments map
    - Convert the HTML fragment to Markdown via MarkItDown and compose headers

    Returns None if no suitable payload is found.
    """
    try:
        payload = None
        # 1) Preferred historic tag
        script = soup.find("script", {"id": "embedded-topic", "type": "json"})
        if script:
            txt = (script.get_text() or "").strip()
            if txt:
                # Try direct JSON first
                try:
                    payload = _json.loads(txt)
                except Exception:
                    # Attempt to extract the first balanced JSON object within CDATA/comment wrappers
                    try:
                        start = txt.find('{')
                        end = txt.rfind('}')
                        if start != -1 and end != -1 and end > start:
                            candidate = txt[start:end+1]
                            payload = _json.loads(candidate)
                        else:
                            payload = None
                    except Exception:
                        payload = None
        # 2) Universal scan of JSON/LD+JSON scripts
        if payload is None:
            for s in soup.find_all("script"):
                t = (s.get("type") or "").strip().lower()
                if t in {"", "json", "application/json", "application/ld+json"}:
                    txt = s.string or ""
                    if not txt.strip():
                        continue
                    try:
                        obj = _json.loads(txt.strip())
                    except Exception:
                        continue
                    # Heuristic: must have some descriptive content
                    if isinstance(obj, dict) and any(k in obj for k in ("description", "articleBody")):
                        payload = obj
                        break
                    # Allow LD-JSON with mainEntity or creativeWork nodes
                    if isinstance(obj, dict):
                        ent = obj.get("mainEntity") or obj.get("article") or obj.get("item")
                        if isinstance(ent, dict) and any(k in ent for k in ("description", "articleBody")):
                            payload = ent
                            break
        if payload is None:
            return None

        # Extract likely fields
        def get_first(d: dict, keys: list) -> str | None:
            return next((d[k] for k in keys if isinstance(d.get(k), str) and d.get(k).strip()), None)
        title = get_first(payload, ["title", "headline", "name", "topic"]) or (soup.title.string if soup.title else None)
        chapter = payload.get("chapter") if isinstance(payload.get("chapter"), str) else None
        subject = payload.get("subject") if isinstance(payload.get("subject"), str) else None
        description_html = get_first(payload, ["description", "articleBody"]) or ""
        attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []

        # Build attachment map: filename -> href
        att_map: dict[str, str] = {}
        for att in attachments:
            try:
                file_name = att.get("file") or ""
                href = att.get("href") or ""
                if file_name and href:
                    att_map[file_name] = href
            except Exception:
                continue

        # Determine base href (e.g., /app/)
        base_tag = soup.find("base")
        base_href = "/"
        try:
            if base_tag and base_tag.has_attr("href"):
                base_href = str(base_tag["href"]).strip() or "/"
        except Exception:
            pass
        # If base doesn't look absolute-like, ensure it begins with a slash
        if not base_href.startswith("http") and not base_href.startswith("/"):
            base_href = "/" + base_href

        # Rewrite inline: references in description to real hrefs (prefix base)
        def _rewrite_inline_refs(html: str) -> str:
            import re as _re

            def _build_full(target: str) -> str:
                # Prefix base (avoid double slashes)
                if base_href.endswith("/") and target.startswith("/"):
                    return base_href[:-1] + target
                if not base_href.endswith("/") and not target.startswith("/"):
                    return base_href + "/" + target
                return base_href + target

            # Replace attributes like src/href="inline:filename"
            def _attr_sub(m: _re.Match[str]) -> str:
                attr = m.group(1)
                fname = m.group(2)
                target = att_map.get(fname)
                if not target:
                    # Keep original inline: reference
                    return f'{attr}="inline:{fname}"'
                return f'{attr}="{_build_full(target)}"'

            html = _re.sub(r'(src|href)=["\']inline:([^"\']+)["\']', _attr_sub, html)

            # Replace bare inline:filename occurrences
            def _bare_sub(m: _re.Match[str]) -> str:
                fname = m.group(1)
                target = att_map.get(fname)
                return _build_full(target) if target else m.group(0)

            html = _re.sub(r'inline:([^\s"\'>)]+)', _bare_sub, html)
            return html

        description_html = _rewrite_inline_refs(description_html)
        # Normalize anchors and general HTML structure using BeautifulSoup
        try:
            _desc_soup = BeautifulSoup(description_html, "lxml")
            description_html = str(_desc_soup)
        except Exception:
            # Fall back to original description_html if parsing fails
            pass

        # Convert the description fragment to Markdown via MarkItDown
        fd, tmp_path = tempfile.mkstemp(suffix=".html")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("<html><head><meta charset='utf-8'></head><body>" + description_html + "</body></html>")
            md = MarkItDown()
            md_result = md.convert(tmp_path)
            desc_md = getattr(md_result, "text_content", "")
            desc_md = desc_md.strip()
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass

        header_lines = []
        if subject:
            header_lines.append(f"# {subject}")
        if chapter and title:
            header_lines.append(f"## {chapter} – {title}")
        elif title:
            header_lines.append(f"# {title}")

        # Compose final markdown
        parts = []
        if header_lines:
            parts.append("\n".join(header_lines))
        if desc_md:
            parts.append(desc_md)

        # Render tagged attachments as semantic sections when available
        def _full_url(href: str) -> str:
            if not href:
                return href
            if href.startswith("http://") or href.startswith("https://"):
                return href
            if href.startswith("/") and base_href.endswith("/"):
                return base_href[:-1] + href
            if href.startswith("/") and not base_href.endswith("/"):
                return base_href + href
            if not href.startswith("/") and not base_href.endswith("/"):
                return base_href + "/" + href
            return base_href + href

        tag_titles = {
            "explanation": "Explanations",
            "idea": "Ideas",
            "usage": "Applications",
        }
        section_items: dict[str, list[str]] = {v: [] for v in tag_titles.values()}
        generic_items: list[str] = []

        for att in attachments:
            try:
                tag = (att.get("tag") or "").strip().lower()
                name = att.get("name") or att.get("file") or ""
                href = att.get("href") or att_map.get(att.get("file") or "", "")
                full = _full_url(href) if href else ""
                if not name:
                    continue
                item = f"- {name}"
                if full:
                    item = f"- [{name}]({full})"
                title = tag_titles.get(tag)
                if title:
                    section_items[title].append(item)
                else:
                    # Skip only if this attachment already appears as a link in the description
                    file_name = (att.get("file") or "").strip()
                    if (href and href in description_html) or (
                        file_name and (
                            f"inline:{file_name}" in description_html or f'"{file_name}"' in description_html or f"'{file_name}'" in description_html
                        )
                    ):
                        continue
                    generic_items.append(item if full else f"- {name}")
            except Exception:
                continue

        for title, items in section_items.items():
            if items:
                parts.append(f"\n**{title}**\n\n" + "\n".join(items))
        if generic_items:
            parts.append("\n**Attachments**\n\n" + "\n".join(generic_items))

        final_md = "\n\n".join(p for p in parts if p)
        if final_md.strip():
            # Apply math/table enhancers for consistency
            final_md = preserve_mathematical_content(final_md)
            final_md = enhance_table_structure(final_md)
            return final_md
        return None
    except Exception:
        return None
