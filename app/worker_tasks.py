"""Internal process entry points; heavy imports are delayed until needed."""

from .deadline import Deadline


def prepare_document(fetched, options, expires_at):
    from .converter import convert_document
    from .links import extract_links_detailed_from_html
    from .markup import decode_text
    from .page_metadata import page_metadata
    from .preflight import needs_browser

    converted = convert_document(
        fetched.data,
        fetched.content_type,
        fetched.final_url,
        html_converter=options.html_converter,
        trafilatura_clean_markdown=options.trafilatura_clean_markdown,
        media_conversion_policy=options.media_conversion_policy,
        timeout_seconds=Deadline.at(expires_at).remaining(),
    )
    links = metadata = None
    if (
        not options.anonymize
        and "html" in (fetched.content_type or "")
        and (options.extract_links or options.extract_metadata)
    ):
        html = decode_text(fetched.data, fetched.content_type)
        if options.extract_links:
            links = extract_links_detailed_from_html(html, fetched.final_url)
        if options.extract_metadata:
            try:
                metadata = page_metadata(html, fetched.final_url)
            except Exception as exc:  # third-party heuristics on page-controlled text; the text stands
                converted.warnings.append(f"Page metadata unavailable ({type(exc).__name__})")
    # Only auto mode can act on the answer, and routing parses again only for a thin page.
    use_browser = options.mode == "auto" and needs_browser(fetched, converted)
    return converted, use_browser, links, metadata


def anonymize_document(text, language):
    from .anonymizer import anonymize

    return anonymize(text, language)
