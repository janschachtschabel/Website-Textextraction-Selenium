"""What each error answer means, for the routes' entries in /docs.

Every error is {"detail": ...}: CrawlError, HTTPException and app/body_limit.py all answer in
that shape. A 422 is the exception to one sentence: a request that does not validate lists
every field at fault, so its model admits both forms.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorAnswer(BaseModel):
    detail: str = Field(description="What went wrong, in one sentence. It never contains text from the page")


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="allow")  # the validator may add context, such as the limit broken

    type: str = Field(description="Kind of error, such as missing, greater_than_equal or url_scheme")
    loc: list[str | int] = Field(description="Where: body, query or path, then the field and any list index")
    msg: str = Field(description="What is wrong with the value, in words")
    input: Any = Field(None, description="The value as it arrived")


class ValidationAnswer(BaseModel):
    detail: str | list[ValidationIssue] = Field(
        description="One entry per field at fault when the request does not validate; one sentence "
        "naming the limit when the service refuses a value itself"
    )


MEANINGS = {
    400: "The URL, a redirect on the way or the address a page finally landed on is not allowed: "
    "it is not http(s), does not resolve, or - under the default network policy - resolves to "
    "a private address. A proxy that fails the same check answers 'Proxy rejected: ...'",
    401: "Only when the operator set API_KEY: the Authorization header carries no Bearer token, or the wrong one",
    403: "respect_robots_txt was set and the host's robots.txt disallows the path",
    404: "No job has this id, or its record expired JOB_RESULT_TTL seconds after the job ended",
    413: "The request body is larger than the operator's MAX_REQUEST_BYTES, 1 MiB unless raised",
    422: "The body or a parameter does not validate, and detail lists each field at fault. Some "
    "values that do validate are refused with one sentence instead: more URLs than "
    "MAX_URLS_PER_REQUEST, a timeout_ms above MAX_TIMEOUT_SECONDS, an invalid CSS selector",
    429: "More crawl requests than the operator's INBOUND_RATE_LIMIT_RPS allows; Retry-After says "
    "how many seconds to wait",
    502: "The page could not be fetched or rendered: the connection, TLS or download failed, the "
    "redirects looped or exceeded ten, Chrome's navigation failed, or a conversion worker failed",
    503: "The service could not do the work: the crawl queue is full, it is shutting down, a worker "
    "process exited, or anonymize was set and the anonymizer failed or its language model is not "
    "installed - the published image carries both",
    504: "timeout_ms ran out - while the URL waited for capacity, for the host's rate limit, or "
    "while it was fetched and converted",
}


# What a status means on routes where the general sentence would say too much or too little.
BATCH_REFUSED = (
    "The body does not validate, and detail lists each field at fault; or it names more URLs than "
    "MAX_URLS_PER_REQUEST or a timeout_ms above MAX_TIMEOUT_SECONDS, refused with one sentence. A URL "
    "that fails once the batch runs carries its error in its own entry instead"
)
TOO_MANY_JOBS = "MAX_ACTIVE_JOBS jobs are already running; submit again once one has ended"
NOT_A_JOB_ID = "The id is not one the service hands out: only letters, digits, - and _, up to 64 characters"
NOT_A_PAGE = NOT_A_JOB_ID + "; or offset is negative, or limit is outside 1 to 100"
STOPPING = (
    "The service is stopping: it finishes the work it has and accepts no more. The body is the one "
    "of a 200, with status 'stopping'"
)


def answers(*statuses: int, specific: dict[int, str] | None = None) -> dict[int, dict[str, Any]]:
    """The responses= of a route: each status with its meaning and the shape of its body.
    ``specific`` replaces a meaning with what that status means on this route."""
    specific = specific or {}
    documented = {
        status: {
            "model": ValidationAnswer if status == 422 else ErrorAnswer,
            "description": specific.get(status, MEANINGS[status]),
        }
        for status in statuses
    }
    if 429 in documented:
        documented[429]["headers"] = {
            "Retry-After": {"description": "Seconds until a request is admitted again", "schema": {"type": "integer"}}
        }
    return documented
