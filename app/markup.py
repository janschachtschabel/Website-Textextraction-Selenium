"""Conservative Markdown postprocessing and source equation preservation."""
import re

from bs4 import BeautifulSoup, UnicodeDammit


def decode_text(data: bytes, content_type: str | None = None) -> str:
    match = re.search(r'charset\s*=\s*["\']?([^;\s"\']+)', content_type or '', re.I)
    encoding = [match.group(1)] if match else []
    return UnicodeDammit(data, known_definite_encodings=encoding, is_html=True).unicode_markup or ''


def enhance_table_structure(text: str) -> str:
    lines = text.split('\n')
    result = []
    fence = None
    in_table = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r'^(`{3,}|~{3,})', stripped):
            marker = stripped[0]
            fence = None if fence == marker else (fence or marker)
        row = not fence and stripped.startswith('|') and stripped.endswith('|')
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ''
        result.append(line)
        if row and not in_table and next_line.startswith('|') and next_line.endswith('|'):
            cells = re.split(r'(?<!\\)\|', stripped)[1:-1]
            next_cells = re.split(r'(?<!\\)\|', next_line)[1:-1]
            separator = all(re.fullmatch(r'\s*:?-{3,}:?\s*', c) for c in next_cells)
            if len(cells) == len(next_cells) and not separator:
                result.append('|' + '|'.join(' --- ' for _ in cells) + '|')
        in_table = row
    return '\n'.join(result)


def prepare_html(data: bytes, content_type: str | None) -> BeautifulSoup:
    soup = BeautifulSoup(decode_text(data, content_type), 'lxml')
    for equation in soup.select('script[type^="math/tex"], math, mjx-container'):
        if not equation.parent:
            continue
        annotation = equation.select_one('annotation[encoding="application/x-tex"]')
        source = annotation.get_text() if annotation else equation.get_text(' ', strip=True)
        if source:
            replacement = soup.new_tag('span')
            replacement.string = '$$' + source + '$$'
            equation.replace_with(replacement)
    for tag in soup.select('[data-tex], [data-latex]'):
        source = tag.get('data-tex') or tag.get('data-latex')
        tag.clear()
        tag.append('$$' + str(source) + '$$')
    return soup
