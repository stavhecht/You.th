from evaluation.trend_check import judge_page, page_text

FILLER = " and more words on a real article page" * 10


def test_a_page_naming_the_subject_verifies() -> None:
    text = (
        page_text("<html><script>x</script><h1>Why Creatine Helps Sleep</h1></html>") + FILLER
    )
    assert judge_page(200, text, "creatine", "Unrelated headline") == "verified"


def test_a_page_carrying_the_headline_verifies_without_the_subject() -> None:
    text = page_text("<p>Five reasons cold plunges are everywhere this winter</p>") + FILLER
    assert (
        judge_page(
            200, text, "cold water immersion", "Five reasons cold plunges are everywhere"
        )
        == "verified"
    )


def test_live_pages_about_something_else_and_failures_are_told_apart() -> None:
    long_text = "an unrelated page about gardening " * 20
    assert judge_page(200, long_text, "creatine", "Creatine and sleep") == "unconfirmed"
    assert judge_page(200, "cookie banner", "creatine", "Creatine and sleep") == "unreadable"
    youtube = "https://www.youtube.com/watch?v=x"
    assert judge_page(200, long_text + " creatine", "creatine", "x", youtube) == "unreadable"
    assert judge_page(403, "", "creatine", "x") == "blocked"
    assert judge_page(404, "", "creatine", "x") == "dead"
    assert judge_page(None, "", "creatine", "x") == "dead"
    assert judge_page(500, "", "creatine", "x") == "error"


def test_only_claim_words_the_subject_does_not_explain_count_as_leaked() -> None:
    from evaluation.image_check import leaked_claim_words

    prompt = "Still life of magnesium tablets, improves nothing, soft light"
    assert leaked_claim_words(
        "Magnesium improves sleep quality", "magnesium", "magnesium for sleep", prompt
    ) == ["improves"]
