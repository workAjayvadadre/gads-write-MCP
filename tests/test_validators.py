"""Field validation against Google's structural limits."""

from __future__ import annotations

from decimal import Decimal

import pytest

from gads_write.safety.validators import (
    ads_char_length,
    validate_budget_units,
    validate_customer_id,
    validate_final_url,
    validate_match_type,
    validate_rsa,
    validate_status,
)

DOMAINS = frozenset({"indiraivf.com", "www.indiraivf.com"})


def _rsa(**overrides):
    base = dict(
        headlines=["Fertility Care", "IVF Specialists", "Book A Consultation"],
        descriptions=[
            "Expert fertility treatment with a track record you can check.",
            "Speak to a specialist near you. Clinics across the country.",
        ],
        final_urls=["https://www.indiraivf.com/treatments"],
        allowed_domains=DOMAINS,
    )
    base.update(overrides)
    return validate_rsa(**base)


# ---------------------------------------------------------------------------
# character counting
# ---------------------------------------------------------------------------

def test_latin_characters_count_as_one() -> None:
    assert ads_char_length("Fertility Care") == 14


def test_devanagari_counts_as_one_so_hindi_copy_measures_normally() -> None:
    text = "बंध्यता"  # 7 code points
    assert ads_char_length(text) == len(text)


def test_cjk_counts_as_two_the_way_google_counts_it() -> None:
    assert ads_char_length("中文") == 4


# ---------------------------------------------------------------------------
# RSA
# ---------------------------------------------------------------------------

def test_valid_rsa_passes() -> None:
    assert _rsa().ok


@pytest.mark.parametrize(
    ("headline", "ok"),
    [("x" * 29, True), ("x" * 30, True), ("x" * 31, False)],
)
def test_headline_length_boundary(headline: str, ok: bool) -> None:
    result = _rsa(headlines=["A", "B", headline])
    assert (not any("headlines[2]" in m for m in result.as_messages())) is ok


@pytest.mark.parametrize(
    ("description", "ok"),
    [("x" * 89, True), ("x" * 90, True), ("x" * 91, False)],
)
def test_description_length_boundary(description: str, ok: bool) -> None:
    result = _rsa(descriptions=["Something useful here.", description])
    assert (not any("descriptions[1]" in m for m in result.as_messages())) is ok


def test_too_few_headlines() -> None:
    result = _rsa(headlines=["One", "Two"])
    assert not result.ok
    assert "at least 3" in result.describe()


def test_too_many_headlines() -> None:
    result = _rsa(headlines=[f"Headline {i}" for i in range(16)])
    assert not result.ok
    assert "at most 15" in result.describe()


def test_too_few_descriptions() -> None:
    result = _rsa(descriptions=["Only one."])
    assert not result.ok
    assert "at least 2" in result.describe()


def test_duplicate_headlines_are_caught() -> None:
    result = _rsa(headlines=["Fertility Care", "fertility care", "Third One"])
    assert not result.ok
    assert "duplicated" in result.describe()


def test_path2_without_path1_is_rejected() -> None:
    result = _rsa(path2="clinics")
    assert not result.ok
    assert "without path1" in result.describe()


def test_path_length_boundary() -> None:
    assert _rsa(path1="x" * 15).ok
    assert not _rsa(path1="x" * 16).ok


def test_all_problems_are_reported_at_once() -> None:
    result = _rsa(headlines=["x" * 40, "y" * 40], descriptions=["z" * 200])
    # too few headlines, 2 over-length headlines, too few descriptions,
    # 1 over-length description
    assert len(result.problems) >= 5


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

def test_http_is_rejected() -> None:
    result = validate_final_url("http://www.indiraivf.com/x", DOMAINS)
    assert not result.ok
    assert "https" in result.describe()


def test_off_domain_url_is_rejected() -> None:
    result = validate_final_url("https://evil.example.com/x", DOMAINS)
    assert not result.ok
    assert "not in the allowed domain list" in result.describe()


def test_subdomain_is_not_implicitly_allowed() -> None:
    # Exact host match. `careers.indiraivf.com` is not `indiraivf.com`.
    assert not validate_final_url("https://careers.indiraivf.com/x", DOMAINS).ok


def test_embedded_credentials_rejected() -> None:
    result = validate_final_url("https://u:p@www.indiraivf.com/x", DOMAINS)
    assert not result.ok
    assert "credentials" in result.describe()


def test_valid_url_passes() -> None:
    assert validate_final_url("https://indiraivf.com/treatments?a=1", DOMAINS).ok


# ---------------------------------------------------------------------------
# enums
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["EXACT", "PHRASE", "BROAD", "  exact  "])
def test_valid_match_types(value: str) -> None:
    assert validate_match_type(value).ok


@pytest.mark.parametrize("value", ["UNKNOWN", "UNSPECIFIED", "EXACT_MATCH", ""])
def test_invalid_match_types(value: str) -> None:
    assert not validate_match_type(value).ok


def test_removed_status_is_refused_with_an_explanation() -> None:
    result = validate_status("REMOVED")
    assert not result.ok
    assert "pause instead" in result.describe()


def test_enabled_and_paused_are_fine() -> None:
    assert validate_status("ENABLED").ok
    assert validate_status("paused").ok


# ---------------------------------------------------------------------------
# ids and amounts
# ---------------------------------------------------------------------------

def test_dashed_customer_id_gets_a_helpful_message() -> None:
    result = validate_customer_id("123-456-7890")
    assert not result.ok
    assert "1234567890" in result.describe()


def test_customer_id_must_be_ten_digits() -> None:
    assert validate_customer_id("1234567890").ok
    assert not validate_customer_id("12345").ok


@pytest.mark.parametrize(
    ("amount", "ok"),
    [("4999.99", True), ("5000", True), ("5000.01", False), ("0", False), ("-5", False)],
)
def test_budget_range_boundary(amount: str, ok: bool) -> None:
    result = validate_budget_units(
        amount, min_units=Decimal(50), max_units=Decimal(5000), currency_code="INR"
    )
    assert result.ok is ok


# ---------------------------------------------------------------------------
# ad group names
# ---------------------------------------------------------------------------

def test_an_ad_group_needs_a_name() -> None:
    from gads_write.safety.validators import validate_ad_group_name

    assert not validate_ad_group_name("").ok
    assert not validate_ad_group_name("   ").ok
    assert validate_ad_group_name("Core Terms").ok


def test_ad_group_name_length_boundary() -> None:
    """255 is one under the 256 Google's System Limits page gives, so this can
    only ever refuse a name Google would have taken."""
    from gads_write.safety.validators import MAX_AD_GROUP_NAME, validate_ad_group_name

    assert MAX_AD_GROUP_NAME == 255
    assert validate_ad_group_name("a" * 255).ok
    assert not validate_ad_group_name("a" * 256).ok


def test_an_ad_group_name_may_not_contain_control_characters() -> None:
    """Google accepts them and then renders them as nothing, producing an ad
    group nobody can find by name."""
    from gads_write.safety.validators import validate_ad_group_name

    assert not validate_ad_group_name("Core" + chr(0) + "Terms").ok


# ---------------------------------------------------------------------------
# location targeting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ordinary", ["Delhi", "New Delhi", "St. Louis", "Washington, D.C.", "Sault Ste-Marie"]
)
def test_ordinary_place_names_are_searchable(ordinary: str) -> None:
    from gads_write.safety.validators import validate_location_query

    assert validate_location_query(ordinary).ok


@pytest.mark.parametrize(
    "hostile",
    [
        "Delhi' OR '1'='1",
        'Delhi"',
        "Delhi\\",
        # LIKE wildcards. They cannot change the query's shape, but they
        # silently change what the pattern MEANS.
        "Del%hi",
        "Del_hi",
        "Del[hi",
        "Del]hi",
        ".Delhi",     # punctuation may not lead
        "",
        "D",          # too short to be a useful search
        "a" * 81,
    ],
)
def test_an_unsearchable_place_name_is_refused(hostile: str) -> None:
    from gads_write.safety.validators import validate_location_query

    assert not validate_location_query(hostile).ok


def test_geo_target_ids_must_be_numeric_and_unique() -> None:
    """Ids rather than names, because "Delhi" is a question - it matches a
    city, a state and a union territory."""
    from gads_write.safety.validators import validate_geo_target_ids

    assert validate_geo_target_ids(["2356", "1007751"]).ok
    assert not validate_geo_target_ids([]).ok
    assert not validate_geo_target_ids(["Delhi"]).ok
    assert not validate_geo_target_ids(["2356", "2356"]).ok
    assert not validate_geo_target_ids("2356").ok      # a string, not a list


def test_too_many_locations_for_one_preview_are_refused() -> None:
    """Not an API limit - a limit on what a human can actually approve. The
    preview names every location, and a preview nobody reads is not an
    approval."""
    from gads_write.safety.validators import (
        MAX_LOCATIONS_PER_CHANGE,
        validate_geo_target_ids,
    )

    at_limit = [str(n) for n in range(MAX_LOCATIONS_PER_CHANGE)]
    assert validate_geo_target_ids(at_limit).ok
    assert not validate_geo_target_ids(at_limit + ["999"]).ok
