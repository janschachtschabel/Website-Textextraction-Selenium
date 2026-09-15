"""Document conversion. HTML never allocates a temporary file."""
import json
import mimetypes
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings
from .html_converter import convert_html, markitdown_stream
from .markup import decode_text, enhance_table_structure  # compatibility export
from .results import ConversionResult


def document_extension(data: bytes, content_type: str | None, url: str | None) -> str:
    mime = (content_type or '').split(';')[0].strip().lower()
    if data.startswith(b'%PDF'):
        return '.pdf'
    if data.lstrip()[:60].lower().startswith((b'<!doctype html', b'<html')):
        return '.html'
    if mime in {'text/html', 'application/xhtml+xml'}:
        return '.html'
    ext = mimetypes.guess_extension(mime) or ''
    if not ext or mime == 'application/octet-stream':
        ext = Path(urlsplit(url or '').path).suffix.lower()
    return ext or '.bin'


def _media_metadata(data: bytes, extension: str, timeout: float) -> ConversionResult:
    try:
        with tempfile.TemporaryDirectory(prefix='extract-media-') as directory:
            path = Path(directory) / ('media' + extension)
            path.write_bytes(data)
            proc = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)],
                capture_output=True, text=True, check=True, timeout=max(0.01, timeout),
            )
        metadata = json.loads(proc.stdout)
        if 'format' in metadata:
            metadata['format'].pop('filename', None)
        return ConversionResult('```json\n' + json.dumps(metadata, ensure_ascii=False, indent=2) + '\n```', 'ffprobe', 'ok')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return ConversionResult(status='failed', warnings=[f'Media metadata unavailable ({type(exc).__name__})'])


def convert_document(data: bytes, content_type: str | None, url: str | None = None, *,
                     html_converter: str | None = None, trafilatura_clean_markdown: bool | None = None,
                     media_conversion_policy: str | None = None, disable_markitdown: bool = False,
                     timeout_seconds: float = 30) -> ConversionResult:
    if not data:
        return ConversionResult()
    mime = (content_type or '').split(';')[0].lower().strip()
    ext = document_extension(data, content_type, url)
    if ext == '.html':
        return convert_html(data, content_type, url, html_converter or settings.html_converter,
                            settings.trafilatura_clean_markdown if trafilatura_clean_markdown is None else trafilatura_clean_markdown)
    if mime.startswith(('video/', 'audio/')):
        policy = media_conversion_policy or settings.media_conversion_policy
        if policy in {'skip', 'none'}:
            return ConversionResult(status='skipped', warnings=['Media conversion disabled by policy'])
        if policy == 'metadata':
            return _media_metadata(data, ext, timeout_seconds)
    if mime.startswith('text/') or ext in {'.txt', '.md', '.csv', '.json', '.xml', '.rss', '.atom'}:
        text = decode_text(data, content_type).strip()
        return ConversionResult(text, 'text', 'ok' if text else 'empty')
    if ext == '.bin' or disable_markitdown:
        return ConversionResult(status='unsupported', warnings=['Unsupported document format'])
    try:
        text = markitdown_stream(data, mime, ext, url)
        return ConversionResult(text, 'markitdown', 'ok' if text else 'empty')
    except Exception as exc:
        return ConversionResult(status='failed', warnings=[f'Document conversion failed ({type(exc).__name__}); check format extras'])


def bytes_to_markdown(data: bytes, content_type: str | None, url: str | None = None, **options) -> str:
    """Compatibility wrapper; the service uses convert_document for status metadata."""
    return convert_document(data, content_type, url, **options).markdown
