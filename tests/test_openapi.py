"""The published schema is the contract, so /docs must offer a request that works.

FastAPI fills an undocumented field with a placeholder for its type. The body that
"Try it out" offered was therefore url="string" with timeout_ms=0, which the
validation rejects - and a reader who corrected only the URL crawled the site with
"User-Agent: string".
"""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.config import settings
from app.main import create_app
from app.schemas import (
    BatchCrawlItemResult,
    BatchCrawlRequest,
    BatchCrawlResponse,
    CrawlRequest,
    CrawlResponse,
    JobAccepted,
    JobStatus,
)

# Without a key the service publishes its schema; that is the surface a reader sees.
SCHEMA = create_app(replace(settings, api_key=None)).openapi()
GENERATED = {"HTTPValidationError", "ValidationError"}  # FastAPI's own, not ours to write
OURS = sorted(name for name in SCHEMA["components"]["schemas"] if name not in GENERATED)
REQUESTS = {"CrawlRequest": CrawlRequest, "BatchCrawlRequest": BatchCrawlRequest}
RESPONSES = {
    "CrawlResponse": CrawlResponse,
    "JobAccepted": JobAccepted,
    "BatchCrawlItemResult": BatchCrawlItemResult,
    "BatchCrawlResponse": BatchCrawlResponse,
    "JobStatus": JobStatus,
}


@pytest.mark.parametrize("name", list(REQUESTS))
def test_the_documented_example_is_a_request_the_service_accepts(name):
    examples = SCHEMA["components"]["schemas"][name].get("examples")
    assert examples, f"{name} publishes no example, so /docs offers type placeholders instead"
    for example in examples:
        REQUESTS[name](**example)  # raises if the body /docs offers would be a 422


@pytest.mark.parametrize("name", OURS)
def test_every_field_says_what_it_is_for(name):
    properties = SCHEMA["components"]["schemas"][name].get("properties", {})
    undocumented = sorted(field for field, spec in properties.items() if not spec.get("description"))
    assert not undocumented, f"{name} leaves {len(undocumented)} fields unexplained: {undocumented}"


def test_every_endpoint_says_what_it_does():
    undocumented = [
        f"{verb.upper()} {path}"
        for path, operations in SCHEMA["paths"].items()
        for verb, operation in operations.items()
        if not operation.get("summary") or not operation.get("description")
    ]
    assert not undocumented, f"without a description /docs shows only the function name: {undocumented}"


def test_the_service_explains_itself_on_its_front_page():
    assert SCHEMA["info"].get("description"), "/docs opens on the title alone"


def test_a_proxy_that_is_not_a_url_is_rejected():
    """The placeholder used to be mapped to "no proxy", which hid a typo as well."""
    with pytest.raises(ValidationError):
        CrawlRequest(url="https://example.com", proxy="string")


@pytest.mark.parametrize("name", list(REQUESTS))
def test_an_unknown_option_is_still_refused(name):
    """The example lives in model_config; overriding it must not drop extra="forbid"."""
    with pytest.raises(ValidationError):
        REQUESTS[name](**{**SCHEMA["components"]["schemas"][name]["examples"][0], "typo": 1})


@pytest.mark.parametrize("name", list(RESPONSES))
def test_the_documented_answer_is_one_the_service_could_have_given(name):
    """Without an example /docs shows an answer of "string" and 0 for every field."""
    examples = SCHEMA["components"]["schemas"][name].get("examples")
    assert examples, f"{name} publishes no example answer"
    for example in examples:
        RESPONSES[name](**example)


def test_the_example_answer_counts_its_own_text_correctly():
    """It was taken from a real crawl; a mangled escape would show up in the length."""
    example = SCHEMA["components"]["schemas"]["CrawlResponse"]["examples"][0]
    assert example["markdown_length"] == len(example["markdown"])
    assert example["word_count"] == len(example["markdown"].split())


def request_examples():
    """Every example /docs offers for a request body, per operation."""
    offered = {}
    for path, operations in SCHEMA["paths"].items():
        for verb, operation in operations.items():
            body = operation.get("requestBody")
            if not body:
                continue
            content = body["content"]["application/json"]
            name = content["schema"]["$ref"].rsplit("/", 1)[-1]
            examples = [entry["value"] for entry in content.get("examples", {}).values()]
            offered[f"{verb.upper()} {path}"] = (name, examples or SCHEMA["components"]["schemas"][name]["examples"])
    return offered


def test_every_request_body_shows_every_option_it_accepts():
    """A minimal example is the one to send; it must not be the only one, or the options
    are only discoverable by leaving "Try it out" for the schema tab."""
    for operation, (name, examples) in request_examples().items():
        fields = set(SCHEMA["components"]["schemas"][name]["properties"])
        shown = set().union(*(set(example) for example in examples))
        assert shown == fields, f"{operation} never shows: {sorted(fields - shown)}"


def test_every_offered_request_example_is_one_the_service_accepts():
    for operation, (name, examples) in request_examples().items():
        assert examples, f"{operation} offers none"
        for example in examples:
            REQUESTS[name](**example)
