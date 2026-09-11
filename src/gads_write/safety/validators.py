"""Field-level validation. Reject bad input before it leaves this process.

The constants here are *Google Ads API structural limits*, not business
policy. That distinction matters: spend limits and account allowlists are
settings because they are deployment-specific; an RSA
headline being 30 characters is a fact about the API and belongs in code.

Verified against Google's documentation:
  - RSA: 3-15 headlines at 30 chars, 2-4 descriptions at 90 chars
  - RSA path1/path2: 15 chars each
  - Characters in double-width scripts (CJK) count as 2

  - Keyword text: 80 characters. Verified against the Google Ads API
    "System limits" page, which pairs it with the error code
    `CriterionError.KEYWORD_TEXT_TOO_LONG`. A word-count limit is widely
    repeated in blog posts but is NOT in Google's own documentation, so it
    is deliberately not enforced here - if one exists, Google rejects the
    mutation and ads/executor.py surfaces that error verbatim.

Python notes for a TypeScript reader:
  - `unicodedata.east_asian_width` returns a two-letter class per character;
    'W' (wide) and 'F' (fullwidth) are the ones Google counts as 2.
  - Functions here *collect* problems into a list rather than raising on the
    first one. Someone fixing an ad wants all the errors at once.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from urllib.parse import urlparse

from .units import MoneyError, coerce_units

# --- Google Ads structural limits ------------------------------------------
RSA_HEADLINE_MAX_CHARS = 30
RSA_DESCRIPTION_MAX_CHARS = 90
RSA_PATH_MAX_CHARS = 15
RSA_MIN_HEADLINES = 3
RSA_MAX_HEADLINES = 15
RSA_MIN_DESCRIPTIONS = 2
RSA_MAX_DESCRIPTIONS = 4

# Verified: developers.google.com/google-ads/api/docs/best-practices/system-limits
# lists "80 characters" for keyword text, with error code
# CriterionError.KEYWORD_TEXT_TOO_LONG.
KEYWORD_MAX_CHARS = 80

# KeywordMatchTypeEnum. UNSPECIFIED and UNKNOWN are real enum members but are
# never valid as *input*; UNKNOWN is documented as a return-only value.
VALID_MATCH_TYPES = frozenset({"EXACT", "PHRASE", "BROAD"})

# CampaignStatusEnum / AdGroupStatusEnum values we permit as input.
# REMOVED is excluded on purpose: removal is irreversible and out of scope
# for v1. See BLOCKED_OPERATIONS in safety/policy.py.
VALID_STATUSES = frozenset({"ENABLED", "PAUSED"})


@dataclass(frozen=True)
class Problem:
    """One thing wrong with the input."""

    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.field}: {self.message}"


@dataclass
class ValidationResult:
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def add(self, field_name: str, message: str) -> None:
        self.problems.append(Problem(field_name, message))

    def extend(self, other: "ValidationResult") -> None:
        self.problems.extend(other.problems)

    def as_messages(self) -> list[str]:
        return [str(p) for p in self.problems]

    def describe(self) -> str:
        if self.ok:
            return "ok"
        return "; ".join(self.as_messages())


# ---------------------------------------------------------------------------
# character counting
# ---------------------------------------------------------------------------

def ads_char_length(text: str) -> int:
    """Length as Google Ads counts it.

    Every character in a double-width language (Korean, Japanese, Chinese)
    counts as two. Devanagari and other Indic scripts are neutral width and
    count as one, so Hindi ad copy is measured normally.
    """
    total = 0
    for char in text:
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


# ---------------------------------------------------------------------------
# individual field validators
# ---------------------------------------------------------------------------

def validate_customer_id(
    customer_id: str, *, field_name: str = "customer_id"
) -> ValidationResult:
    """Ten digits, no dashes. The dashed form from the UI is a common paste error."""
    result = ValidationResult()
    text = str(customer_id).strip()
    if "-" in text:
        result.add(
            field_name,
            f"{text!r} contains dashes. Use the digits only, e.g. "
            f"{text.replace('-', '')}",
        )
    elif not text.isdigit() or len(text) != 10:
        result.add(field_name, f"{text!r} is not a 10-digit customer ID")
    return result


def validate_match_type(
    match_type: str, *, field_name: str = "match_type"
) -> ValidationResult:
    result = ValidationResult()
    text = str(match_type).strip().upper()
    if text not in VALID_MATCH_TYPES:
        result.add(
            field_name,
            f"{match_type!r} is not a usable match type. "
            f"Expected one of {sorted(VALID_MATCH_TYPES)}",
        )
    return result


def validate_status(status: str, *, field_name: str = "status") -> ValidationResult:
    result = ValidationResult()
    text = str(status).strip().upper()
    if text == "REMOVED":
        result.add(
            field_name,
            "REMOVED is not permitted. Removal is irreversible and is out of "
            "scope for v1; pause instead.",
        )
    elif text not in VALID_STATUSES:
        result.add(field_name, f"{status!r} is not one of {sorted(VALID_STATUSES)}")
    return result


def validate_keyword_text(
    text: object, *, field_name: str = "keyword_text"
) -> ValidationResult:
    """Non-empty, within Google's 80-character keyword limit.

    Counted the way Google counts, so CJK characters cost two. Leading and
    trailing whitespace is not an error but is not counted either, because
    the executor sends the stripped text.

    Match-type punctuation is rejected. In the Google Ads UI you type
    "shoes" for broad, "shoes" in quotes for phrase and [shoes] for exact,
    but the API takes the bare text plus a separate match_type field.
    Passing the UI syntax through would create a keyword that literally
    contains brackets, which silently matches nothing.
    """
    result = ValidationResult()
    if not isinstance(text, str):
        result.add(field_name, f"expected text, got {type(text).__name__}")
        return result

    stripped = text.strip()
    if not stripped:
        result.add(field_name, "must not be empty")
        return result

    if stripped[0] in "[\"'" or stripped[-1] in "]\"'":
        result.add(
            field_name,
            f"{stripped!r} looks like Google Ads UI match-type syntax. Pass the "
            "bare keyword text and set match_type separately; brackets and "
            "quotes would become part of the keyword itself.",
        )
        return result

    length = ads_char_length(stripped)
    if length > KEYWORD_MAX_CHARS:
        detail = (
            f"{length} characters as Google counts them "
            f"({len(stripped)} code points)"
            if length != len(stripped)
            else f"{length} characters"
        )
        result.add(field_name, f"{detail}, limit is {KEYWORD_MAX_CHARS}")

    return result


def validate_final_url(
    url: str,
    allowed_domains: frozenset[str] | set[str],
    *,
    field_name: str = "final_url",
) -> ValidationResult:
    """https, on an allowlisted host, no credentials embedded.

    The domain allowlist is the thing that stops an ad in your account
    pointing somewhere it should not.
    """
    result = ValidationResult()
    text = str(url).strip()

    try:
        parsed = urlparse(text)
    except ValueError:
        result.add(field_name, f"{text!r} is not a parsable URL")
        return result

    if parsed.scheme != "https":
        result.add(
            field_name,
            f"must use https, got {parsed.scheme or 'no scheme'} in {text!r}",
        )

    if parsed.username or parsed.password:
        result.add(field_name, "must not embed credentials in the URL")

    host = (parsed.hostname or "").lower()
    if not host:
        result.add(field_name, f"{text!r} has no host")
    elif host not in {d.lower() for d in allowed_domains}:
        result.add(
            field_name,
            f"host {host!r} is not in the allowed domain list "
            f"{sorted(allowed_domains)}. Add it to GADS_ALLOWED_URL_DOMAINS if "
            "it is legitimate.",
        )

    return result


def validate_budget_units(
    amount_units: object,
    *,
    min_units: Decimal,
    max_units: Decimal,
    currency_code: str = "",
    field_name: str = "daily_budget",
) -> ValidationResult:
    """Range check on a currency-unit amount.

    Boundaries are INCLUSIVE: a budget exactly equal to max_units is allowed.
    Tests pin this at just-under / exactly-at / just-over.
    """
    result = ValidationResult()
    try:
        value = coerce_units(amount_units, field=field_name)
    except MoneyError as exc:
        result.add(field_name, str(exc).split(": ", 1)[-1])
        return result

    suffix = f" {currency_code}" if currency_code else ""
    if value <= 0:
        result.add(field_name, f"must be greater than zero, got {value}{suffix}")
    elif value < min_units:
        result.add(
            field_name,
            f"{value}{suffix} is below the minimum of {min_units}{suffix}",
        )
    elif value > max_units:
        result.add(
            field_name,
            f"{value}{suffix} exceeds the maximum of {max_units}{suffix}",
        )

    return result


# ---------------------------------------------------------------------------
# responsive search ads
# ---------------------------------------------------------------------------

def validate_rsa(
    *,
    headlines: list[str],
    descriptions: list[str],
    final_urls: list[str],
    allowed_domains: frozenset[str] | set[str],
    path1: str | None = None,
    path2: str | None = None,
) -> ValidationResult:
    """Validate a responsive search ad against Google's structural limits."""
    result = ValidationResult()

    # --- headlines ---
    if len(headlines) < RSA_MIN_HEADLINES:
        result.add(
            "headlines", f"needs at least {RSA_MIN_HEADLINES}, got {len(headlines)}"
        )
    if len(headlines) > RSA_MAX_HEADLINES:
        result.add(
            "headlines", f"allows at most {RSA_MAX_HEADLINES}, got {len(headlines)}"
        )
    for index, text in enumerate(headlines):
        _check_text(result, f"headlines[{index}]", text, RSA_HEADLINE_MAX_CHARS)

    duplicates = _duplicates(headlines)
    if duplicates:
        result.add("headlines", f"duplicated: {sorted(duplicates)}")

    # --- descriptions ---
    if len(descriptions) < RSA_MIN_DESCRIPTIONS:
        result.add(
            "descriptions",
            f"needs at least {RSA_MIN_DESCRIPTIONS}, got {len(descriptions)}",
        )
    if len(descriptions) > RSA_MAX_DESCRIPTIONS:
        result.add(
            "descriptions",
            f"allows at most {RSA_MAX_DESCRIPTIONS}, got {len(descriptions)}",
        )
    for index, text in enumerate(descriptions):
        _check_text(result, f"descriptions[{index}]", text, RSA_DESCRIPTION_MAX_CHARS)

    duplicates = _duplicates(descriptions)
    if duplicates:
        result.add("descriptions", f"duplicated: {sorted(duplicates)}")

    # --- paths ---
    for label, value in (("path1", path1), ("path2", path2)):
        if value:
            _check_text(result, label, value, RSA_PATH_MAX_CHARS)
    if path2 and not path1:
        result.add("path2", "cannot be set without path1")

    # --- urls ---
    if not final_urls:
        result.add("final_urls", "at least one final URL is required")
    for index, url in enumerate(final_urls):
        result.extend(
            validate_final_url(url, allowed_domains, field_name=f"final_urls[{index}]")
        )

    return result


def _check_text(
    result: ValidationResult, field_name: str, text: object, limit: int
) -> None:
    if not isinstance(text, str):
        result.add(field_name, f"expected text, got {type(text).__name__}")
        return
    stripped = text.strip()
    if not stripped:
        result.add(field_name, "must not be empty")
        return
    length = ads_char_length(stripped)
    if length > limit:
        # Report both counts when they differ, otherwise the message looks
        # wrong to someone counting characters by eye in a CJK string.
        detail = (
            f"{length} characters as Google counts them "
            f"({len(stripped)} code points)"
            if length != len(stripped)
            else f"{length} characters"
        )
        result.add(field_name, f"{detail}, limit is {limit}")


# ---------------------------------------------------------------------------
# reporting inputs
# ---------------------------------------------------------------------------
# These exist because GAQL is assembled by string interpolation: the Google
# Ads API takes a query as a string and offers no bound parameters, so there
# is no `?` placeholder to hide behind. Anything that reaches a query is
# checked here first and asserted AGAIN in ads/reads.py. Two layers on
# purpose - this one produces a message a human can act on, that one is a
# backstop against a validation call somebody forgot to make.
#
# Date constants like LAST_30_DAYS are deliberately not accepted. Dates are
# computed in Python against the account's own timezone and passed as
# literal ISO dates, so the server never depends on GAQL grammar it has not
# verified, and "yesterday" means yesterday in Mumbai rather than in UTC.

MAX_REPORT_DAYS = 365
MAX_REPORT_ROWS = 1000

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_iso_date(value: object, *, field_name: str) -> ValidationResult:
    """Exactly YYYY-MM-DD, and a real calendar date.

    The regex alone would accept 2026-02-31; `date.fromisoformat` is what
    rejects it. Both are needed: the regex pins the shape that reaches the
    query, the parse pins the meaning.
    """
    result = ValidationResult()
    text = str(value).strip()
    if not _ISO_DATE.match(text):
        result.add(field_name, f"{text!r} is not a date in YYYY-MM-DD form")
        return result
    try:
        date.fromisoformat(text)
    except ValueError:
        result.add(field_name, f"{text!r} is not a real calendar date")
    return result


def validate_date_range(
    start_date: object,
    end_date: object,
    *,
    max_days: int = MAX_REPORT_DAYS,
) -> ValidationResult:
    """A well-formed, correctly ordered, bounded date range."""
    result = ValidationResult()
    result.extend(validate_iso_date(start_date, field_name="start_date"))
    result.extend(validate_iso_date(end_date, field_name="end_date"))
    if not result.ok:
        return result

    start = date.fromisoformat(str(start_date).strip())
    end = date.fromisoformat(str(end_date).strip())

    if start > end:
        result.add(
            "start_date", f"{start} is after end_date {end}"
        )
        return result

    span_days = (end - start).days + 1
    if span_days > max_days:
        result.add(
            "start_date",
            f"the range {start}..{end} covers {span_days} days; the maximum is "
            f"{max_days}. Narrow the range.",
        )
    return result


def validate_row_limit(
    limit: object, *, maximum: int = MAX_REPORT_ROWS, field_name: str = "limit"
) -> ValidationResult:
    """A positive row count within a bound.

    Bounded because an unbounded report is a way to turn one tool call into
    a very large Google Ads response and a very large model context.
    """
    result = ValidationResult()
    if isinstance(limit, bool) or not isinstance(limit, int):
        # bool is a subclass of int in Python, and True would silently become
        # a LIMIT of 1.
        result.add(field_name, f"must be a whole number, got {limit!r}")
        return result
    if limit < 1:
        result.add(field_name, f"must be at least 1, got {limit}")
    elif limit > maximum:
        result.add(field_name, f"must be at most {maximum}, got {limit}")
    return result


def validate_numeric_id(value: object, *, field_name: str) -> ValidationResult:
    """Digits only. Used for campaign and ad group IDs in report filters."""
    result = ValidationResult()
    text = str(value).strip()
    if not text.isdigit():
        result.add(field_name, f"{text!r} is not a numeric ID")
    return result


def _duplicates(items: list[str]) -> set[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        key = item.strip().casefold()
        if key in seen:
            dupes.add(item.strip())
        seen.add(key)
    return dupes


# ---------------------------------------------------------------------------
# free-form GAQL
# ---------------------------------------------------------------------------
# `run_gaql_query` hands the model the query language itself, which is a
# deliberate departure from every other read here. Those interpolate a handful
# of validated values into a query this code owns; this one does not own the
# query at all.
#
# What still holds, and it is the part that matters: the account is checked
# against the MCC by the gate before a query runs, and every call is made with
# the CALLER'S own OAuth token. Google therefore enforces what that person may
# read, exactly as it does in the Google Ads UI. This validation is
# defence-in-depth over that, not the primary control.
#
# Injection in the SQL sense does not apply. GAQL has no JOINs, no subqueries
# and no statement chaining - one SELECT against one resource - and it reaches
# Google through `search()`, which cannot mutate. The architecture test pins
# that mutations live only in ads/executor.py.
#
# So what is left to check is SCOPE: which resource is being read.

# Resources a read tool has no business reaching. A denylist rather than an
# allowlist, deliberately: an allowlist would have to enumerate a hundred
# resources to be useful and would then need maintaining every time Google
# adds one - which is the config-file problem this project spent its life
# removing. These are the ones that expose people and payment plumbing rather
# than advertising performance.
DENIED_GAQL_RESOURCES = frozenset(
    {
        "customer_user_access",
        "customer_user_access_invitation",
        "billing_setup",
        "account_budget",
        "account_budget_proposal",
        "payments_account",
        "invoice",
    }
)

# Enough rows for any real question, few enough that one query cannot pull an
# account's entire history into a chat.
MAX_GAQL_ROWS = 500

_SELECT_FROM = re.compile(
    r"^\s*SELECT\s+.+?\s+FROM\s+([a-z_][a-z0-9_]*)\b", re.IGNORECASE | re.DOTALL
)
_LIMIT = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)


def gaql_resource(query: str) -> str | None:
    """The resource named in the FROM clause, or None if the query is not a
    single well-formed SELECT."""
    match = _SELECT_FROM.match(str(query or ""))
    return match.group(1).lower() if match else None


def validate_gaql_query(query: object) -> ValidationResult:
    """Check a caller-supplied GAQL query before it reaches Google."""
    result = ValidationResult()
    text = str(query or "").strip()

    if not text:
        result.add("query", "a GAQL query is required")
        return result

    if len(text) > 4000:
        result.add("query", "query is unreasonably long; simplify it")
        return result

    # One statement. GAQL has no statement chaining, so a semicolon is either
    # a mistake or an attempt at something this does not support.
    if ";" in text:
        result.add("query", "a query must be a single statement with no ';'")
        return result

    resource = gaql_resource(text)
    if resource is None:
        result.add(
            "query",
            "must be a single SELECT ... FROM <resource> query. GAQL has no "
            "JOINs or subqueries; select the fields you need from one resource.",
        )
        return result

    if resource in DENIED_GAQL_RESOURCES:
        result.add(
            "query",
            f"reading {resource!r} is not available through this server. It "
            "exposes people or payment details rather than advertising "
            "performance; use the Google Ads UI if you genuinely need it.",
        )

    # PARAMETERS must come after LIMIT in GAQL, so a query carrying one cannot
    # simply have a LIMIT appended. Rather than rewrite someone's clause order,
    # refuse and let them add their own.
    if re.search(r"\bPARAMETERS\b", text, re.IGNORECASE):
        result.add(
            "query",
            "PARAMETERS is not supported here; add an explicit LIMIT instead.",
        )
        return result

    limit = _LIMIT.search(text)
    if limit and int(limit.group(1)) > MAX_GAQL_ROWS:
        result.add(
            "query",
            f"LIMIT {limit.group(1)} is above the maximum of {MAX_GAQL_ROWS}",
        )

    return result


def with_row_limit(query: str) -> str:
    """The query, guaranteed to carry a LIMIT.

    An unbounded query against a large account is how a chat window fills
    with a year of data and a request times out. Validation has already
    refused a LIMIT that is too high, so an existing one is left alone.
    """
    text = str(query).strip()
    if _LIMIT.search(text):
        return text
    return f"{text} LIMIT {MAX_GAQL_ROWS}"


# ---------------------------------------------------------------------------
# creating a campaign
# ---------------------------------------------------------------------------

# Google's own limit on a campaign name. Verified on the API's System Limits
# page against CampaignError.DUPLICATE_CAMPAIGN_NAME being a separate error -
# length and uniqueness are different failures.
MAX_CAMPAIGN_NAME = 255

# What this server will create. Two of the seventeen the API offers; see
# ads/executor.py:CAMPAIGN_BIDDING_STRATEGIES for why.
CAMPAIGN_BIDDING_STRATEGIES = ("MANUAL_CPC", "MAXIMIZE_CLICKS")


def validate_campaign_name(name: object) -> ValidationResult:
    result = ValidationResult()
    text = str(name or "").strip()
    if not text:
        result.add("name", "a campaign needs a name")
        return result
    if len(text) > MAX_CAMPAIGN_NAME:
        result.add(
            "name",
            f"a campaign name may be at most {MAX_CAMPAIGN_NAME} characters, "
            f"got {len(text)}",
        )
    # Control characters would be accepted by Google and then render as
    # nothing in the UI, producing a campaign nobody can find by name.
    if any(unicodedata.category(ch).startswith("C") for ch in text):
        result.add("name", "a campaign name may not contain control characters")
    return result


def validate_bidding_strategy(strategy: object) -> ValidationResult:
    result = ValidationResult()
    text = str(strategy or "").strip().upper()
    if text not in CAMPAIGN_BIDDING_STRATEGIES:
        result.add(
            "bidding_strategy",
            f"bidding_strategy must be one of "
            f"{list(CAMPAIGN_BIDDING_STRATEGIES)}, got {strategy!r}. "
            "Manual CPC lets you set bids yourself; Maximize Clicks lets "
            "Google spend the budget on clicks.",
        )
    return result


# ---------------------------------------------------------------------------
# creating an ad group
# ---------------------------------------------------------------------------

# Google's System Limits page gives 256 characters for an ad group name,
# paired with the error code `AdGroupError.INVALID_ADGROUP_NAME`. 255 is used
# here so the two name limits in this file agree: being one character
# stricter than Google can only ever refuse a name Google would have taken,
# never accept one it would reject.
MAX_AD_GROUP_NAME = 255


# ---------------------------------------------------------------------------
# location targeting
# ---------------------------------------------------------------------------

MIN_LOCATION_QUERY = 2
MAX_LOCATION_QUERY = 80

# How many locations one change may carry. Not an API limit - the API takes
# far more - but a limit on what a human can actually approve. The preview
# names every location, and a preview nobody reads is not an approval.
MAX_LOCATIONS_PER_CHANGE = 20

# Mirrors _SAFE_TEXT in ads/reads.py, which asserts the same thing again.
# Only the first character is pinned to alphanumeric: the safety property is
# the character set, and "Washington, D.C." ends in a period.
_LOCATION_QUERY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .,&-]*$")


def validate_location_query(text: object) -> ValidationResult:
    """A place name to search for, safe to put inside a GAQL LIKE pattern.

    Deliberately a narrow allowlist rather than an escaping scheme. GAQL
    string literals are quoted, and the grammar escapes the LIKE wildcards
    (`%`, `_`, `[`, `]`) by bracketing them - rules we have not verified and
    are not going to implement blind. So the characters that would matter are
    simply not allowed through.

    The cost is a name containing an apostrophe. Geo target constant names
    are English by definition (the v25 proto says "Geo target constant English
    name"), so this loses "Cote d'Ivoire" and very little else. Asserted again
    by `_text_literal` in ads/reads.py.
    """
    result = ValidationResult()
    value = str(text or "").strip()
    if not value:
        result.add("query", "a place name is required")
        return result
    if len(value) < MIN_LOCATION_QUERY:
        result.add(
            "query",
            f"{value!r} is too short to search on; use at least "
            f"{MIN_LOCATION_QUERY} characters",
        )
        return result
    if len(value) > MAX_LOCATION_QUERY:
        result.add(
            "query",
            f"a place name may be at most {MAX_LOCATION_QUERY} characters, "
            f"got {len(value)}",
        )
        return result
    if not _LOCATION_QUERY.fullmatch(value):
        result.add(
            "query",
            f"{value!r} contains characters this search cannot pass to Google "
            "safely. Use letters, digits, spaces, and . , & - only.",
        )
    return result


def validate_geo_target_ids(ids: object) -> ValidationResult:
    """A non-empty, deduplicated list of numeric geo target constant ids.

    Ids rather than names, on purpose. "Delhi" is a city, a state and a union
    territory in Google's data, so a name is not an answer - it is a question.
    `find_locations` turns the question into a set of ids and shows a person
    which is which; this refuses anything less definite.
    """
    result = ValidationResult()
    if not isinstance(ids, (list, tuple)):
        result.add("location_ids", f"expected a list, got {type(ids).__name__}")
        return result
    values = [str(value).strip() for value in ids]
    if not values:
        result.add("location_ids", "at least one location id is required")
        return result
    if len(values) > MAX_LOCATIONS_PER_CHANGE:
        result.add(
            "location_ids",
            f"at most {MAX_LOCATIONS_PER_CHANGE} locations per change, got "
            f"{len(values)}. The preview has to name every one of them, and a "
            "preview nobody reads is not an approval.",
        )
        return result
    for index, value in enumerate(values):
        if not value.isdigit():
            result.add(
                f"location_ids[{index}]",
                f"{value!r} is not a numeric geo target constant id. Use "
                "find_locations to look one up.",
            )
    duplicates = _duplicates(values)
    if duplicates:
        result.add("location_ids", f"duplicated: {sorted(duplicates)}")
    return result


def validate_ad_group_name(name: object) -> ValidationResult:
    """Same shape as validate_campaign_name, for the same reasons.

    Not shared with it deliberately: the two limits come from different rows
    of Google's System Limits page and different error codes, so folding them
    into one function would make a future divergence invisible.
    """
    result = ValidationResult()
    text = str(name or "").strip()
    if not text:
        result.add("name", "an ad group needs a name")
        return result
    if len(text) > MAX_AD_GROUP_NAME:
        result.add(
            "name",
            f"an ad group name may be at most {MAX_AD_GROUP_NAME} characters, "
            f"got {len(text)}",
        )
    # Control characters would be accepted by Google and then render as
    # nothing in the UI, producing an ad group nobody can find by name.
    if any(unicodedata.category(ch).startswith("C") for ch in text):
        result.add("name", "an ad group name may not contain control characters")
    return result
