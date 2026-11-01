from __future__ import annotations

import os
import re
import tomllib
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import openai
import pytest

# openai 3.x ships httpx2; older releases use httpx. Only used to build fake
# error responses, never for real requests.
try:
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx

import llm
from batcher import ContextLine, SourceEntry, make_batches
from helpers import make_batch

THAI_RE = re.compile(r"[\u0e00-\u0e7f]")
CONFIG = Path(__file__).parent.parent / "config.toml"


# --- encode -----------------------------------------------------------------


def test_encode_entries():
    entries = [SourceEntry(12, "Hello there."), SourceEntry(13, "Fine.")]
    assert llm.encode_entries(entries) == "[12] Hello there.\n[13] Fine."


def test_encode_keeps_multiline_entries():
    entries = [SourceEntry(1, "Hello there.\nHow are you?")]
    assert llm.encode_entries(entries) == "[1] Hello there.\nHow are you?"


def test_encode_empty_batch():
    assert llm.encode_entries([]) == ""


# --- decode -----------------------------------------------------------------


def test_decode_simple_reply():
    decoded = llm.decode_entries("[12] สวัสดี\n[13] สบายดี")
    assert decoded.entries == {12: "สวัสดี", 13: "สบายดี"}
    assert decoded.issues == []


def test_decode_joins_continuation_lines():
    decoded = llm.decode_entries("[1] สวัสดี\nHow are you?\n[2] จบ")
    assert decoded.entries[1] == "สวัสดี\nHow are you?"
    assert decoded.entries[2] == "จบ"


def test_decode_ignores_text_before_the_first_entry():
    decoded = llm.decode_entries("Here you go:\n[1] สวัสดี")
    assert decoded.entries == {1: "สวัสดี"}


def test_decode_reports_duplicate_and_keeps_first():
    decoded = llm.decode_entries("[1] แรก\n[1] สอง")
    assert decoded.entries == {1: "แรก"}
    assert [i.kind for i in decoded.issues] == ["duplicate"]


def test_decode_reports_empty_entry():
    decoded = llm.decode_entries("[1] สวัสดี\n[2]   \n[3] จบ")
    assert decoded.entries[2] == ""
    assert [(i.kind, i.index) for i in decoded.issues] == [("empty", 2)]


def test_decode_raises_when_nothing_is_tagged():
    with pytest.raises(llm.BadResponse):
        llm.decode_entries("I cannot help with that.")


def test_decode_roundtrip_between_encode_and_decode():
    entries = [SourceEntry(5, "One."), SourceEntry(6, "Two.\nSecond line.")]
    decoded = llm.decode_entries(llm.encode_entries(entries) + "\n")
    assert decoded.entries == {5: "One.", 6: "Two.\nSecond line."}


def test_decode_tolerates_windows_line_endings():
    decoded = llm.decode_entries("[1] สวัสดี\r\n[2] จบ\r\n")
    assert decoded.entries == {1: "สวัสดี", 2: "จบ"}


# --- prompt -----------------------------------------------------------------


def test_build_messages_has_system_and_user():
    messages = llm.build_messages(make_batch(2))
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == llm.SYSTEM_PROMPT


def test_build_messages_contains_tagged_entries():
    user = llm.build_messages(make_batch(2))[1]["content"]
    assert "[TRANSLATE]" in user
    assert "[1] Line 1" in user
    assert "[2] Line 2" in user


def test_build_messages_omits_empty_bible_and_glossary():
    user = llm.build_messages(make_batch(1))[1]["content"]
    assert "STORY BIBLE" not in user
    assert "GLOSSARY" not in user


def test_build_messages_includes_bible_and_glossary():
    user = llm.build_messages(make_batch(1), bible="A space film.", glossary="NASA = keep")[1]["content"]
    assert "[STORY BIBLE]\nA space film." in user
    assert "[GLOSSARY]\nNASA = keep" in user


def test_build_messages_marks_context_as_untranslatable():
    batch = make_batch(2)
    batch.before = [ContextLine(0, "Earlier.")]
    batch.after = [ContextLine(3, "Later.")]
    user = llm.build_messages(batch)[1]["content"]
    assert "[CONTEXT BEFORE - do not translate]" in user
    assert "[CONTEXT AFTER - do not translate]" in user


def test_build_messages_includes_thai_in_before_context_only():
    batch = make_batch(2)
    batch.before = [ContextLine(0, "Earlier.", thai="ก่อนหน้านี้")]
    batch.after = [ContextLine(3, "Later.", thai=None)]
    user = llm.build_messages(batch)[1]["content"]
    before_block, after_block = user.split("[CONTEXT AFTER - do not translate]")
    assert "TH: ก่อนหน้านี้" in before_block
    assert "TH:" not in after_block


def test_system_prompt_states_the_core_rules():
    prompt = llm.SYSTEM_PROMPT
    assert "Thai" in prompt
    assert "One input ID = one output ID" in prompt
    assert "must not appear in your output" in prompt
    assert "{0}" in prompt


def test_system_prompt_asks_for_transliteration():
    prompt = llm.SYSTEM_PROMPT
    assert "Write everything in Thai script" in prompt
    assert "เมย่า: ไม่" in prompt  # a label keeps its colon
    assert "ฟลายมยเอมีโบร22" in prompt


def test_system_prompt_keeps_numbers_and_links_verbatim():
    prompt = llm.SYSTEM_PROMPT
    assert "Numbers stay exactly as written" in prompt
    assert "@handles" in prompt


def test_system_prompt_states_the_alphanumeric_threshold():
    prompt = llm.SYSTEM_PROMPT
    assert "shorter than 5" in prompt
    assert "K191" in prompt


def test_system_prompt_allows_an_official_title_but_forbids_inventing_one():
    assert "Never invent an official-sounding title" in llm.SYSTEM_PROMPT


def test_prompt_version_covers_the_transliteration_policy():
    assert llm.PROMPT_VERSION >= 2


# --- api key ----------------------------------------------------------------


def test_load_api_key_reads_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-123")
    assert llm.load_api_key() == "sk-test-123"


def test_load_api_key_strips_whitespace(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "  sk-test-123\n")
    assert llm.load_api_key() == "sk-test-123"


def test_load_api_key_custom_env_name(monkeypatch):
    monkeypatch.setenv("OTHER_PROVIDER_KEY", "abc")
    assert llm.load_api_key("OTHER_PROVIDER_KEY") == "abc"


def test_load_api_key_missing_raises_auth_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(llm.AuthError) as excinfo:
        llm.load_api_key()
    assert "OPENROUTER_API_KEY" in str(excinfo.value)


def test_key_pasted_into_key_env_is_rejected_without_echoing_it(monkeypatch):
    pasted = "sk-or-v1-placeholder-not-a-key"
    with pytest.raises(llm.AuthError) as excinfo:
        llm.load_api_key(pasted)
    assert "sk-or-" not in str(excinfo.value)
    assert "NAME of the variable" in str(excinfo.value)


def test_missing_key_message_never_contains_a_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    with pytest.raises(llm.AuthError) as excinfo:
        llm.load_api_key()
    assert "sk-" not in str(excinfo.value)


# --- error mapping ----------------------------------------------------------


def status_error(cls, status: int, headers: dict | None = None):
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return cls("boom", response=response, body=None)


def client_raising(exc: Exception) -> llm.OpenRouterClient:
    """An OpenRouterClient whose SDK call always fails with `exc`."""
    client = object.__new__(llm.OpenRouterClient)
    client.model = "test/model"
    client.temperature = 0.25

    def create(**kwargs):
        raise exc

    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return client


def client_returning(content: str, prompt_tokens: int = 7, completion_tokens: int = 5):
    client = object.__new__(llm.OpenRouterClient)
    client.model = "test/model"
    client.temperature = 0.25
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        ),
    )
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response))
    )
    return client


def test_complete_returns_text_and_usage():
    completion = client_returning("[1] สวัสดี").complete([{"role": "user", "content": "x"}])
    assert completion.text == "[1] สวัสดี"
    assert completion.usage.prompt_tokens == 7
    assert completion.usage.completion_tokens == 5


def test_rate_limit_becomes_rate_limited_with_retry_after():
    client = client_raising(status_error(openai.RateLimitError, 429, {"retry-after": "7"}))
    with pytest.raises(llm.RateLimited) as excinfo:
        client.complete([])
    assert excinfo.value.retry_after == 7.0


def test_rate_limit_without_header_has_no_retry_after():
    client = client_raising(status_error(openai.RateLimitError, 429))
    with pytest.raises(llm.RateLimited) as excinfo:
        client.complete([])
    assert excinfo.value.retry_after is None


def test_auth_failure_becomes_auth_error():
    client = client_raising(status_error(openai.AuthenticationError, 401))
    with pytest.raises(llm.AuthError):
        client.complete([])


def test_timeout_becomes_transport_error():
    client = client_raising(openai.APITimeoutError(request=httpx.Request("POST", "https://x")))
    with pytest.raises(llm.TransportError):
        client.complete([])


def test_connection_failure_becomes_transport_error():
    client = client_raising(
        openai.APIConnectionError(request=httpx.Request("POST", "https://x"))
    )
    with pytest.raises(llm.TransportError):
        client.complete([])


def test_server_error_is_retryable():
    client = client_raising(status_error(openai.InternalServerError, 500))
    with pytest.raises(llm.TransportError):
        client.complete([])


@pytest.mark.parametrize("status", [502, 503, 504, 408, 409, 425])
def test_transient_statuses_are_retryable(status):
    cls = openai.InternalServerError if status >= 500 else openai.APIStatusError
    client = client_raising(status_error(cls, status))
    with pytest.raises(llm.TransportError):
        client.complete([])


def test_bad_request_stays_fatal():
    client = client_raising(status_error(openai.BadRequestError, 400))
    with pytest.raises(llm.LLMError) as excinfo:
        client.complete([])
    assert not isinstance(excinfo.value, llm.RetryableError)


def test_unavailable_keeps_retry_after():
    client = client_raising(status_error(openai.InternalServerError, 503, {"retry-after": "12"}))
    with pytest.raises(llm.TransportError) as excinfo:
        client.complete([])
    assert excinfo.value.retry_after == 12.0


@pytest.mark.parametrize("header", ["soon", "-5", "1e999", "nan", ""])
def test_unusable_retry_after_is_ignored(header):
    client = client_raising(status_error(openai.RateLimitError, 429, {"retry-after": header}))
    with pytest.raises(llm.RateLimited) as excinfo:
        client.complete([])
    assert excinfo.value.retry_after is None


def test_retry_after_accepts_an_http_date():
    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    client = client_raising(status_error(openai.RateLimitError, 429, {"retry-after": when}))
    with pytest.raises(llm.RateLimited) as excinfo:
        client.complete([])
    assert 25 <= excinfo.value.retry_after <= 30


def test_client_sets_timeout_and_leaves_retrying_to_us(monkeypatch):
    import openai

    captured: dict = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    llm.OpenRouterClient(model="m/x", api_key="test-key", timeout_s=42)
    assert captured["timeout"] == 42
    assert captured["max_retries"] == 0  # the runner owns the retry budget
    assert captured["base_url"] == llm.DEFAULT_BASE_URL


def test_empty_choices_is_bad_response():
    client = object.__new__(llm.OpenRouterClient)
    client.model, client.temperature = "test/model", 0.25
    response = SimpleNamespace(choices=[], usage=None)
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: response))
    )
    with pytest.raises(llm.BadResponse):
        client.complete([])


# --- live -------------------------------------------------------------------


@pytest.mark.live
def test_live_smoke_translates_three_entries():
    """Real OpenRouter call. Run with: pytest -m live"""
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set")

    settings = tomllib.loads(CONFIG.read_text())["llm"]
    client = llm.OpenRouterClient(
        model=settings["model"],
        base_url=settings["base_url"],
        temperature=settings["temperature"],
        timeout_s=settings["timeout_s"],
        key_env=settings["key_env"],
    )

    entries = [
        SourceEntry(1, "Hello there."),
        SourceEntry(2, "How are you?"),
        SourceEntry(3, "<i>Fine, thanks.</i>"),
    ]
    batch = make_batches(entries, size=3)[0]
    reply = client.complete(llm.build_messages(batch))
    decoded = llm.decode_entries(reply.text)

    assert set(decoded.entries) == {1, 2, 3}
    for index, text in decoded.entries.items():
        assert THAI_RE.search(text), f"entry {index} has no Thai: {text!r}"


def test_system_prompt_states_the_line_count_rule():
    prompt = llm.SYSTEM_PROMPT
    assert "two lines" in prompt
    assert "line count" in prompt
    # A label must not be pushed onto a line of its own.
    assert "never put the label alone on a line" in prompt


def test_strict_messages_add_the_correction_rules():
    normal = llm.build_messages(make_batch(1))[0]["content"]
    strict = llm.build_messages(make_batch(1), strict=True)[0]["content"]
    assert strict.startswith(normal)
    assert llm.FIX_RULES.strip() in strict
    assert "correction pass" in strict


def test_non_strict_messages_do_not_mention_a_correction():
    assert "correction pass" not in llm.build_messages(make_batch(1))[0]["content"]


def test_review_messages_show_only_the_current_translations_given():
    messages = llm.build_messages(make_batch(2), current={1: "สวัสดี"})
    user = messages[1]["content"]
    section = user.split("[CURRENT TRANSLATIONS - flagged, improve or repeat]\n")[1]
    section = section.split("\n\n")[0]
    assert section == "[1] สวัสดี"


def test_review_messages_use_the_review_rules():
    messages = llm.build_messages(make_batch(1), current={1: "สวัสดี"})
    system = messages[0]["content"]
    assert llm.REVIEW_RULES.strip() in system
    assert llm.FIX_RULES.strip() not in system
    assert "review pass" in system


def test_current_without_matching_entries_adds_no_section():
    user = llm.build_messages(make_batch(1), current={99: "อื่น"})[1]["content"]
    assert "CURRENT TRANSLATIONS" not in user


def test_extra_headers_are_sent_as_default_headers(monkeypatch):
    import openai

    captured: dict = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    llm.OpenRouterClient(
        model="m/x", api_key="test-key", extra_headers={"HTTP-Referer": "https://me.test"}
    )
    assert captured["default_headers"] == {"HTTP-Referer": "https://me.test"}


def test_no_extra_headers_means_no_default_headers(monkeypatch):
    import openai

    captured: dict = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    llm.OpenRouterClient(model="m/x", api_key="test-key")
    assert captured["default_headers"] is None


@pytest.mark.parametrize(
    "value, expected",
    [
        ("OPENROUTER_API_KEY", False),
        ("sk-" + "a" * 30, True),
        ("sk-or-v1-" + "b" * 30, True),
        ("Bearer sk-" + "a" * 30, False),
    ],
)
def test_looks_like_key(value, expected):
    assert llm.looks_like_key(value) is expected


# --- tokens that must survive translation -----------------------------------


def test_kept_verbatim_lists_links_handles_and_short_codes():
    source = "See https://example.com/x @flymiamibro22 or mail a@b.example, K9, 3B."
    kept = llm.kept_verbatim(source)
    assert "https://example.com/x" in kept
    assert "@flymiamibro22" in kept
    assert "a@b.example" in kept
    assert "K9" in kept
    assert "3B" in kept


def test_a_long_letter_digit_code_is_not_kept_verbatim():
    assert llm.kept_verbatim("Flymiamibro22") == []
    assert llm.kept_verbatim("21L4") == ["21L4"]  # 4 characters stays Latin


def test_must_keep_digits_lists_every_number_and_the_digits_of_a_long_code():
    assert llm.must_keep_digits("06:00") == ["06", "00"]
    assert llm.must_keep_digits("Flymiamibro22") == ["22"]
    assert llm.must_keep_digits("seventh grade") == []


def test_a_digit_spelled_as_a_word_is_reported():
    assert llm.missing_kept('"Pen15"', '"เพนฟิฟทีน"') == ["15"]
    assert llm.missing_kept('"Pen15"', '"เพน15"') == []


def test_a_short_code_must_come_back_as_written():
    assert llm.missing_kept("K9", "เค9") == ["K9"]
    assert llm.missing_kept("K9", "K9") == []


def test_a_link_must_come_back_unchanged():
    source = "https://example.com/x"
    assert llm.missing_kept(source, "ดูที่ https://example.com/x") == []
    assert llm.missing_kept(source, "ดูที่ example.com") == [source]


def test_a_url_does_not_take_the_comma_after_it():
    assert llm.kept_verbatim("See https://example.com, then") == [
        "https://example.com"
    ]


def test_an_email_does_not_take_the_period_after_it():
    assert llm.kept_verbatim("Write to desk@nightshift.example.com.") == [
        "desk@nightshift.example.com",
        "@nightshift",
    ]


def test_a_url_keeps_the_slash_that_belongs_to_it():
    assert llm.kept_verbatim("See https://example.com/, then") == [
        "https://example.com/"
    ]


def test_a_url_followed_by_thai_punctuation_is_not_a_lost_token():
    source = "That's https://nightshift.example.com, right?"
    assert llm.missing_kept(source, "นั่น https://nightshift.example.com ใช่ไหม") == []


def test_an_email_followed_by_thai_punctuation_is_not_a_lost_token():
    source = "Write to maya@nightshift.example.com."
    assert llm.missing_kept(source, "เขียนถึง maya@nightshift.example.com") == []


def test_validate_reports_a_lost_number():
    from batcher import make_batches, SourceEntry

    batch = make_batches([SourceEntry(1, '"Pen15"')], size=1)[0]
    decoded = llm.decode_entries('[1] "เพนฟิฟทีน"')
    kinds = [issue.kind for issue in llm.validate(batch, decoded)]
    assert "lost_token" in kinds


def test_validate_accepts_a_kept_number():
    from batcher import make_batches, SourceEntry

    batch = make_batches([SourceEntry(1, '"Pen15"')], size=1)[0]
    decoded = llm.decode_entries('[1] "เพน15"')
    assert llm.validate(batch, decoded) == []


# --- latin glued to thai ----------------------------------------------------


def test_mixed_script_finds_a_latin_run_glued_to_thai():
    # South Park [841] came back as "ทwerp!".
    assert llm.mixed_script("ทwerp!") == ["werp"]


def test_mixed_script_sees_a_run_behind_several_thai_characters():
    # Not limited to one Thai letter: Thai words are longer than that.
    assert llm.mixed_script("ข้อความwerp") == ["werp"]


def test_mixed_script_ignores_a_delimited_word():
    assert llm.mixed_script("สวัสดี werp") == []
    assert llm.mixed_script("werp!") == []


def test_mixed_script_ignores_a_kept_code_touching_thai():
    # Thai has no spaces, so a short code may legitimately abut Thai.
    assert llm.kept_verbatim("V2") == ["V2"]
    assert llm.mixed_script("ใช้V2") == []


def test_mixed_script_strips_markup_before_looking():
    assert llm.mixed_script("<i>ทwerp</i>") == ["werp"]
    assert llm.mixed_script("<i>สวัสดี</i>") == []


def test_validate_reports_latin_glued_to_thai():
    from batcher import make_batches, SourceEntry

    batch = make_batches([SourceEntry(841, "Bullshit.")], size=1)[0]
    decoded = llm.decode_entries("[841] ทwerp!")
    kinds = [issue.kind for issue in llm.validate(batch, decoded)]
    assert "mixed_script" in kinds


# --- usage reporting --------------------------------------------------------


def test_usage_reads_reasoning_tokens_and_the_reported_cost():
    response = SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=50,
            cost=0.002,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=40),
            prompt_tokens_details=SimpleNamespace(cached_tokens=10),
        )
    )
    usage = llm.usage_from_response(response)
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 50  # already includes the reasoning
    assert usage.reasoning_tokens == 40
    assert usage.cached_tokens == 10
    assert usage.cost_usd == pytest.approx(0.002)


def test_usage_without_details_is_still_readable():
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=6)
    )
    usage = llm.usage_from_response(response)
    assert (usage.reasoning_tokens, usage.cached_tokens) == (0, 0)
    assert usage.cost_usd is None


def test_usage_reads_fields_the_sdk_did_not_model():
    usage_object = SimpleNamespace(prompt_tokens=1, completion_tokens=2)
    usage_object.model_extra = {
        "cost": 0.5,
        "completion_tokens_details": {"reasoning_tokens": 7},
    }
    usage = llm.usage_from_response(SimpleNamespace(usage=usage_object))
    assert usage.reasoning_tokens == 7
    assert usage.cost_usd == pytest.approx(0.5)


def test_a_reply_without_usage_reports_nothing():
    assert llm.usage_from_response(SimpleNamespace()) == llm.Usage()


def test_usage_adds_every_field_and_keeps_a_known_cost():
    first = llm.Usage(prompt_tokens=1, completion_tokens=2, reasoning_tokens=3, cost_usd=0.1)
    second = llm.Usage(prompt_tokens=10, completion_tokens=20, reasoning_tokens=30)
    total = first + second
    assert total.prompt_tokens == 11
    assert total.completion_tokens == 22
    assert total.reasoning_tokens == 33
    assert total.cost_usd == pytest.approx(0.1)


def test_usage_adds_two_known_costs():
    total = llm.Usage(cost_usd=0.1) + llm.Usage(cost_usd=0.2)
    assert total.cost_usd == pytest.approx(0.3)


# --- more lines than the source ---------------------------------------------


def test_validate_reports_more_lines_than_the_source():
    batch = make_batches([SourceEntry(1, "One.\nTwo.")], size=1)[0]
    decoded = llm.decode_entries("[1] หนึ่ง\nสอง\nสาม")
    kinds = [issue.kind for issue in llm.validate(batch, decoded)]
    assert "extra_lines" in kinds


def test_validate_accepts_the_source_line_count():
    batch = make_batches([SourceEntry(1, "One.\nTwo.")], size=1)[0]
    decoded = llm.decode_entries("[1] หนึ่ง\nสอง")
    assert llm.validate(batch, decoded) == []
