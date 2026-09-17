"""Internal process entry points; heavy imports are delayed until needed."""

from .deadline import Deadline


def prepare_document(fetched, options, expires_at):
    from .converter import convert_document
    from .links import extract_links_detailed_from_html
    from .markup import decode_text
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
    links = None
    if options.extract_links and not options.anonymize and "html" in (fetched.content_type or ""):
        links = extract_links_detailed_from_html(decode_text(fetched.data, fetched.content_type), fetched.final_url)
    # Routing parses the document again, and only auto mode can act on the answer.
    use_browser = options.mode == "auto" and needs_browser(fetched, converted)
    return converted, use_browser, links


def anonymize_document(text, language):
    from .anonymizer import anonymize

    return anonymize(text, language)
