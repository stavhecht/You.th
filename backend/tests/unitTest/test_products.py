"""Product extraction checks, identity, status ladders, official pages, openFDA."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.intel.reader import ReaderOutput, validate, worth_reading
from app.products.fda import lookup_510k, parse_date
from app.products.resolve import (
    checked_category,
    is_official_url,
    is_product_page,
    launch_status,
    product_key,
    product_status,
)
from app.social.contracts import CollectionError, CollectionStatus
from app.social.weekly import suppression_reason

NOW = datetime(2026, 11, 4, tzinfo=UTC)


def _reading(**item) -> ReaderOutput:
    base = {
        "id": 1,
        "is_wellness_topic": True,
        "subject": "",
        "outcome": "",
        "category": "other",
    }
    return ReaderOutput.model_validate({"items": [{**base, **item}]})


def _product(**fields) -> dict:
    base = {
        "brand": "Oura",
        "product_name": "Oura Ring 4",
        "category": "smart_ring",
        "mention_kind": "review",
        "quote": "Oura Ring 4",
    }
    return {**base, **fields}


# --- extraction checks -----------------------------------------------------------


def test_a_product_quoted_from_the_text_is_kept() -> None:
    text = "I wore the Oura Ring 4 for a month: sleep tracking review"
    out = validate(_reading(subject="sleep tracking", products=[_product()]), {1: text})
    assert out[1]["products"][0]["product_name"] == "Oura Ring 4"
    assert out[1]["topic"]["subject"] == "sleep tracking"


def test_a_product_the_text_does_not_name_is_dropped() -> None:
    text = "Best smart rings for sleep"
    out = validate(_reading(products=[_product()]), {1: text})
    assert out[1]["products"] == []


def test_a_quote_without_the_brand_is_dropped() -> None:
    text = "The Ring 4 is here, and Oura says it is lighter"
    out = validate(_reading(products=[_product(quote="Ring 4")]), {1: text})
    assert out[1]["products"] == []


def test_an_invented_id_and_a_repeated_one_are_ignored() -> None:
    output = ReaderOutput.model_validate(
        {
            "items": [
                {"id": 9, "is_wellness_topic": True, "subject": "x", "category": "other"},
                {
                    "id": 1,
                    "is_wellness_topic": True,
                    "subject": "creatine",
                    "category": "supplements",
                },
                {"id": 1, "is_wellness_topic": True, "subject": "sleep", "category": "other"},
            ]
        }
    )
    out = validate(output, {1: "Creatine before bed"})
    assert list(out) == [1]
    assert out[1]["topic"]["subject"] == "creatine"


def test_a_subject_not_in_the_text_is_no_topic() -> None:
    out = validate(_reading(subject="ashwagandha"), {1: "Morning routine vlog"})
    assert out[1]["topic"] is None


def test_a_non_wellness_text_keeps_its_products_but_no_topic() -> None:
    out = validate(
        _reading(is_wellness_topic=False, subject="oura", products=[_product()]),
        {1: "Oura Ring 4 unboxing"},
    )
    assert out[1]["topic"] is None and len(out[1]["products"]) == 1


def test_the_word_filter_keeps_wellness_and_product_texts_only() -> None:
    assert worth_reading("Magnesium for sleep, does it work?")
    assert worth_reading("New smart ring unveiled at CES")
    assert not worth_reading("Celebrity wedding photos")


# --- identity and status -----------------------------------------------------------


@pytest.mark.parametrize(
    ("brand", "name"),
    [
        ("Oura", "Oura Ring 4"),
        ("OURA", "Ring Gen 4"),
        ("Oura™", "Ring 4th generation"),
        ("oura", "ring   4"),
        ("Oura", "Oura Ring Gen4"),
    ],
)
def test_spellings_of_one_product_share_a_key(brand: str, name: str) -> None:
    assert product_key(brand, name) == "oura|ring 4"


def test_a_brand_alone_is_not_a_product() -> None:
    assert product_key("Whoop", "Whoop") is None
    assert product_key("", "Ring") is None


def test_status_climbs_and_never_falls() -> None:
    assert product_status("uncertain", independent_sources=1, official_url=None, fda=False) == (
        "uncertain"
    )
    assert product_status("uncertain", independent_sources=2, official_url=None, fda=False) == (
        "corroborated"
    )
    assert product_status(
        "corroborated", independent_sources=0, official_url=None, fda=False
    ) == ("corroborated")
    assert product_status("uncertain", independent_sources=1, official_url="x", fda=False) == (
        "confirmed"
    )


def test_a_mention_is_not_a_launch() -> None:
    assert launch_status("none", launch_sources=0, fda_decision=None, now=NOW) == "none"
    assert launch_status("none", launch_sources=1, fda_decision=None, now=NOW) == "claimed"
    assert launch_status("none", launch_sources=2, fda_decision=None, now=NOW) == "corroborated"
    assert (
        launch_status("none", launch_sources=0, fda_decision=NOW - timedelta(days=30), now=NOW)
        == "confirmed"
    )
    old = NOW - timedelta(days=800)
    assert launch_status("none", launch_sources=0, fda_decision=old, now=NOW) == "none"


@pytest.mark.parametrize(
    ("url", "brand", "official"),
    [
        ("https://ouraring.com/product/rings", "Oura", True),
        ("https://www.whoop.com/us/en/", "WHOOP", True),
        ("https://shop.therabody.com/x", "Therabody", True),
        ("https://www.dexcom.co.uk/stelo", "Dexcom", True),
        ("https://appleinsider.com/articles/x", "Apple", False),
        ("https://www.amazon.com/Oura-Ring/dp/B0", "Oura", False),
        ("https://ouraringreviews.net/", "Oura", False),
    ],
)
def test_an_official_page_is_on_the_brands_own_domain(
    url: str, brand: str, official: bool
) -> None:
    assert is_official_url(url, brand) is official


# --- openFDA ----------------------------------------------------------------------


class FakeHttp:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.params: dict | None = None

    async def get(self, url: str, *, params=None, headers=None) -> httpx.Response:
        self.params = params
        return self.response


async def test_a_510k_record_matching_the_applicant_confirms() -> None:
    body = {
        "results": [
            {
                "k_number": "K1",
                "applicant": "Other Co",
                "device_name": "Stelo",
                "decision_date": "2024-03-05",
            },
            {
                "k_number": "K2",
                "applicant": "DexCom, Inc.",
                "device_name": "Stelo Glucose Biosensor System",
                "decision_date": "20240305",
            },
        ]
    }
    http = FakeHttp(httpx.Response(200, content=json.dumps(body).encode()))
    reference = await lookup_510k(http, brand="Dexcom", name="stelo")
    assert reference is not None and reference["k_number"] == "K2"
    assert reference["decision_date"].startswith("2024-03-05")
    assert 'applicant:"Dexcom"' in http.params["search"]
    # Updates get clearances of their own, so only the first can date a launch.
    assert http.params["sort"] == "decision_date:asc"


async def test_no_510k_is_none_and_a_throttle_is_rate_limited() -> None:
    not_found = httpx.Response(404, content=b'{"error": {"code": "NOT_FOUND"}}')
    assert await lookup_510k(FakeHttp(not_found), brand="Oura", name="ring 4") is None
    with pytest.raises(CollectionError) as caught:
        await lookup_510k(FakeHttp(httpx.Response(429)), brand="Oura", name="ring 4")
    assert caught.value.status is CollectionStatus.RATE_LIMITED


def test_both_openfda_date_shapes_parse() -> None:
    assert (
        parse_date("2024-03-05") == parse_date("20240305") == datetime(2024, 3, 5, tzinfo=UTC)
    )
    assert parse_date("March") is None


# --- suppression (novelty only) -----------------------------------------------------


def test_a_promoted_topic_is_never_proposed_again() -> None:
    reason = suppression_reason(
        "promoted",
        NOW - timedelta(days=400),
        creators_then=1,
        creators_now=50,
        now=NOW,
        cooloff_days=28,
    )
    assert reason is not None and "promoted" in reason


def test_a_dismissed_topic_returns_when_its_creators_double_or_the_cooloff_ends() -> None:
    recent = NOW - timedelta(days=5)
    held = suppression_reason(
        "dismissed", recent, creators_then=4, creators_now=7, now=NOW, cooloff_days=28
    )
    assert held is not None
    assert (
        suppression_reason(
            "dismissed", recent, creators_then=4, creators_now=8, now=NOW, cooloff_days=28
        )
        is None
    )
    assert (
        suppression_reason(
            "dismissed",
            NOW - timedelta(days=40),
            creators_then=4,
            creators_now=1,
            now=NOW,
            cooloff_days=28,
        )
        is None
    )


@pytest.mark.parametrize(
    ("category", "text", "expected"),
    [
        ("smart_ring", "Oura Ring 5", "smart_ring"),
        ("smart_ring", "Huawei Watch D3 for blood pressure", "other"),
        ("smart_ring", "the HUAWEI FreeClip 2 earbuds", "other"),
        ("smart_ring", "the spring sale, during which", "other"),  # word starts only
        ("glucose_monitor", "Titan Smart Home Gym", "other"),
        ("massage_gun", "Nurri milk", "other"),
        ("massage_gun", "Theragun Pro massager review", "massage_gun"),
        # The name alone says nothing; the text around it does.
        ("glucose_monitor", "Dexcom Stelo: a CGM you buy yourself", "glucose_monitor"),
        ("other", "Anything", "other"),
        ("not_a_category", "Oura Ring", "other"),
    ],
)
def test_the_readers_category_stands_only_if_the_source_calls_it_that(
    category: str, text: str, expected: str
) -> None:
    assert checked_category(category, text) == expected


@pytest.mark.parametrize(
    ("brand", "name", "url", "expected"),
    [
        # The product pages behind real confirmations, 2026-11-07.
        ("Oura", "Oura Ring 5", "https://ouraring.com/store/rings/oura-ring-5", True),
        ("Huawei", "Watch D3", "https://consumer.huawei.com/uk/wearables/watch-d3/", True),
        ("Huawei", "Watch Fit 5", "https://consumer.huawei.com/uk/wearables/watch-fit5/", True),
        ("HUAWEI", "FreeClip 2", "https://consumer.huawei.com/bh/headphones/freeclip2/", True),
        ("Samsung", "Galaxy Buds", "https://www.samsung.com/us/audio/galaxy-buds/", True),
        ("Oura", "Ring Gen 3", "https://ouraring.com/store/oura-ring-gen3", True),
        # Category pages that confirmed product lines, the same day.
        ("Samsung", "Galaxy watches", "https://www.samsung.com/us/watches/", False),
        (
            "Samsung",
            "Galaxy wearables",
            "https://www.samsung.com/us/mobile/wearables/all-wearables/",
            False,
        ),
        ("Meta", "smart glasses", "https://www.meta.com/ai-glasses/", False),
        # The price of being strict: nothing but the brand and the kind, or an opaque path.
        ("Aina", "Aina Ring", "https://shop.aina.com/", False),
        ("Oura", "Oura Ring 5", "https://ouraring.com/p/123", False),
    ],
)
def test_a_product_page_holds_the_words_that_say_which_product(
    brand: str, name: str, url: str, expected: bool
) -> None:
    assert is_product_page(url, brand, name) is expected
