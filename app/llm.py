# This module has been removed.
# LLM post-processing and anonymization are replaced by Microsoft Presidio.
# See app/anonymizer.py for the new implementation.
raise ImportError(
    "app.llm has been removed. Use app.anonymizer for PII anonymization."
)

from __future__ import annotations

SYSTEM_PROMPT = (
    "Du bist ein Assistent, der Markdown-Texte bereinigt und klassifiziert. "
    "Reinige den Text, korrigiere Markdown-Strukturen, entferne offensichtliche Navigations-/Werbe-/Cookie-Hinweise. "
    "Arbeite nur den relevanten Artikel- oder Inhaltskern heraus. "
    "Klassifiziere das Ergebnis in genau eine der Kategorien: "
    "'Bildungsinhalt' (Markdown des Bildungsinhalts selbst), "
    "'Metabeschreibung' (beschreibende Infos über Bildungsinhalte, aber nicht der Inhalt selbst), "
    "'Fehler/Infoseite' (z.B. 404, Wartung, Zugriff verweigert). "
    "Gib ausschließlich JSON im folgenden Format zurück: {\n"
    "  \"cleaned_markdown\": string,\n"
    "  \"classification\": \"Bildungsinhalt|Metabeschreibung|Fehler/Infoseite\",\n"
    "  \"anonymized\": boolean\n"
    "}"
)


def _strip_code_fences(text: str) -> str:
    """Remove surrounding triple backtick code fences if present."""
    if not isinstance(text, str):
        return text
    # Match ```lang\n...``` or ```...```
    m = re.match(r"^```[a-zA-Z0-9_-]*\n([\s\S]*?)```\s*$", text.strip())
    if m:
        return m.group(1).strip()
    return text


def _extract_json_object(s: str) -> dict:
    """Best-effort extraction of a JSON object from arbitrary LLM text.

    - Strips code fences
    - Tries full-string json.loads
    - Falls back to a brace-counting scan that correctly handles nested braces
      and } characters inside string values (unlike non-greedy regex).
    """
    if not isinstance(s, str):
        return {}
    s1 = _strip_code_fences(s)
    # Try direct parse first
    try:
        return json.loads(s1)
    except Exception:
        pass
    # Brace-counting extractor: finds every top-level {...} block correctly
    try:
        i = 0
        while i < len(s1):
            if s1[i] != '{':
                i += 1
                continue
            depth = 0
            in_str = False
            escape = False
            j = i
            while j < len(s1):
                ch = s1[j]
                if escape:
                    escape = False
                elif ch == '\\' and in_str:
                    escape = True
                elif ch == '"':
                    in_str = not in_str
                elif not in_str:
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            block = s1[i:j + 1]
                            try:
                                obj = json.loads(block)
                                if isinstance(obj, dict) and 'cleaned_markdown' in obj:
                                    return obj
                            except Exception:
                                pass
                            break
                j += 1
            i += 1
        # Last-resort: span from first { to last }
        first = s1.find('{')
        last = s1.rfind('}')
        if first != -1 and last > first:
            return json.loads(s1[first:last + 1])
    except Exception:
        pass
    return {}


def _flatten_cleaned_markdown(value: str) -> str:
    """Ensure cleaned_markdown is plain markdown, not code-fenced JSON or nested JSON.

    - Strip code fences
    - If the result looks like JSON and has a cleaned_markdown key, extract it
    - Finally, if still fenced (markdown fences), strip again
    """
    if not isinstance(value, str):
        return value
    text = _strip_code_fences(value)
    # If the text itself is JSON with cleaned_markdown, unwrap
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and 'cleaned_markdown' in obj:
            inner = obj.get('cleaned_markdown', '')
            return _strip_code_fences(inner or '')
    except Exception:
        pass
    return text


def _supports_new_params(model: str) -> bool:
    """True if the model accepts reasoning/verbosity via responses.create."""
    return "gpt-5" in model.lower()


def _build_user_prompt(markdown: str, clean_prompt: str | None, anonymize: bool) -> str:
    extra = (clean_prompt or "").strip()
    if anonymize:
        extra += "\nFühre zusätzlich eine Anonymisierung personenbezogener Daten durch."
    return f"Bereinige folgenden Markdown-Inhalt. {extra}\n---\n{markdown}\n---\n"


def _parse_llm_response(
    content: str | None,
    original_markdown: str,
    anonymize: bool,
) -> tuple[str, str, bool, int | None]:
    """Shared post-processing logic for both sync and async LLM calls."""
    cleaned = original_markdown
    classification = "Metabeschreibung"
    anonymized = anonymize
    try:
        data = _extract_json_object(content or "")
        if data:
            new_cleaned = data.get("cleaned_markdown")
            if isinstance(new_cleaned, str):
                cleaned = _flatten_cleaned_markdown(new_cleaned) or cleaned
            classification = data.get("classification", classification) or classification
            anonymized = bool(data.get("anonymized", anonymized))
        else:
            raise ValueError("no_json")
    except Exception:
        if isinstance(content, str) and content.strip():
            cleaned = _strip_code_fences(content.strip())
    return cleaned, classification, anonymized, None


def postprocess_markdown(
    *,
    markdown: str,
    base_url: str | None,
    api_key: str,
    model: str,
    base: str | None = None,
    clean_prompt: str | None = None,
    anonymize: bool = False,
    reasoning_effort: str | None = None,
    verbosity: str | None = None,
) -> tuple[str, str, bool, int | None]:
    """
    Returns: cleaned_markdown, classification, anonymized, tokens_used
    """
    client = OpenAI(api_key=api_key, base_url=base or None)
    user_prompt = _build_user_prompt(markdown, clean_prompt, anonymize)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    # Build extra kwargs for gpt-5 family (reasoning + verbosity controls)
    extra: dict = {}
    if _supports_new_params(model):
        if reasoning_effort:
            extra["reasoning"] = {"effort": reasoning_effort}
        if verbosity:
            extra["text"] = {"verbosity": verbosity}

    content: str | None = None
    tokens_used: int | None = None
    try:
        resp = client.responses.create(model=model, input=messages, **extra)  # type: ignore[arg-type]
        content = resp.output_text  # type: ignore[attr-defined]
        usage = getattr(resp, "usage", None)
        tokens_used = getattr(usage, "total_tokens", None) if usage else None
    except Exception:
        chat = client.chat.completions.create(model=model, messages=messages)  # type: ignore[arg-type]
        content = chat.choices[0].message.content if chat.choices else ""
        tokens_used = getattr(chat, "usage", None).total_tokens if getattr(chat, "usage", None) else None

    cleaned, classification, anonymized, _ = _parse_llm_response(content, markdown, anonymize)
    return cleaned, classification, anonymized, tokens_used


async def postprocess_markdown_async(
    *,
    markdown: str,
    base_url: str | None,
    api_key: str,
    model: str,
    base: str | None = None,
    clean_prompt: str | None = None,
    anonymize: bool = False,
    reasoning_effort: str | None = None,
    verbosity: str | None = None,
) -> tuple[str, str, bool, int | None]:
    """
    Async variant to prevent blocking the event loop.
    Returns: cleaned_markdown, classification, anonymized, tokens_used
    """
    client = AsyncOpenAI(api_key=api_key, base_url=base or None)
    user_prompt = _build_user_prompt(markdown, clean_prompt, anonymize)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    # Build extra kwargs for gpt-5 family (reasoning + verbosity controls)
    extra: dict = {}
    if _supports_new_params(model):
        if reasoning_effort:
            extra["reasoning"] = {"effort": reasoning_effort}
        if verbosity:
            extra["text"] = {"verbosity": verbosity}

    content: str | None = None
    tokens_used: int | None = None
    try:
        resp = await client.responses.create(model=model, input=messages, **extra)  # type: ignore[arg-type]
        content = resp.output_text  # type: ignore[attr-defined]
        usage = getattr(resp, "usage", None)
        tokens_used = getattr(usage, "total_tokens", None) if usage else None
    except Exception:
        chat = await client.chat.completions.create(model=model, messages=messages)  # type: ignore[arg-type]
        content = chat.choices[0].message.content if chat.choices else ""
        tokens_used = getattr(chat, "usage", None).total_tokens if getattr(chat, "usage", None) else None

    cleaned, classification, anonymized, _ = _parse_llm_response(content, markdown, anonymize)
    return cleaned, classification, anonymized, tokens_used
