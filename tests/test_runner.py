from __future__ import annotations

import pytest

from khamsub import llm
from khamsub import runner
from khamsub.batcher import Batch, ContextLine, SourceEntry, make_batches


def batch_of(pairs: list[tuple[int, str]], **kwargs) -> Batch:
    entries = [SourceEntry(index, text) for index, text in pairs]
    batch = make_batches(entries, size=max(len(entries), 1))[0]
    for key, value in kwargs.items():
        setattr(batch, key, value)
    return batch


class Recorder:
    """Captures sleep calls so delays can be asserted without waiting."""

    def __init__(self):
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def run(batch, client, **kwargs):
    recorder = Recorder()
    kwargs.setdefault("sleep", recorder)
    kwargs.setdefault("rand", lambda: 0.5)  # no jitter
    result = runner.translate_batch(client, batch, **kwargs)
    return result, recorder


# --- happy path -------------------------------------------------------------


def test_all_good_makes_one_call(fake_client):
    fake_client.queue("[1] สวัสดี\n[2] ลาก่อน")
    result, recorder = run(batch_of([(1, "Hello."), (2, "Goodbye.")]), fake_client)

    assert fake_client.call_count == 1
    assert recorder.delays == []
    assert result.as_dict() == {1: "สวัสดี", 2: "ลาก่อน"}
    assert result.failures == []


def test_empty_batch_makes_no_calls(fake_client):
    result, _ = run(Batch(), fake_client)
    assert fake_client.call_count == 0
    assert result.entries == []


def test_first_call_uses_client_temperature_and_retries_use_low(fake_client):
    fake_client.queue("[1] สวัสดี")  # entry 2 missing, so one retry happens
    fake_client.queue("[2] ลาก่อน")
    run(batch_of([(1, "Hello."), (2, "Goodbye.")]), fake_client)
    assert fake_client.temperatures == [None, 0.1]


def test_usage_accumulates_across_calls(fake_client):
    fake_client.queue(llm.Completion("[1] สวัสดี", llm.Usage(10, 5)))
    fake_client.queue(llm.Completion("[2] ลาก่อน", llm.Usage(4, 2)))
    result, _ = run(batch_of([(1, "Hello."), (2, "Goodbye.")]), fake_client)
    assert result.usage.prompt_tokens == 14
    assert result.usage.completion_tokens == 7


def test_the_batch_records_who_served_each_request(fake_client):
    fake_client.queue(
        llm.Completion("[1] สวัสดี\n[2] ลาก่อน", provider="DeepInfra")
    )
    result, _ = run(batch_of([(1, "Hello."), (2, "Goodbye.")]), fake_client)
    assert result.providers == {"DeepInfra": 1}


def test_a_retry_on_another_provider_is_counted_too(fake_client):
    fake_client.queue(llm.Completion("[2] ลาก่อน", provider="DeepInfra"))  # forgot [1]
    fake_client.queue(llm.Completion("[1] สวัสดี", provider="DeepSeek"))
    result, _ = run(batch_of([(1, "Hello."), (2, "Goodbye.")]), fake_client)
    assert result.providers == {"DeepInfra": 1, "DeepSeek": 1}


# --- retrying only what failed ----------------------------------------------


def test_only_the_missing_entry_is_re_requested(fake_client):
    fake_client.queue("[2] ลาก่อน")  # forgot [1]
    fake_client.queue("[1] สวัสดี")
    result, _ = run(batch_of([(1, "Hello."), (2, "Goodbye.")]), fake_client)

    assert fake_client.call_count == 2
    second_call = fake_client.calls[1][1]["content"]
    assert "[TRANSLATE]\n[1] Hello." in second_call
    assert "[2] Goodbye." not in second_call
    assert result.as_dict() == {1: "สวัสดี", 2: "ลาก่อน"}


def test_failure_reason_is_recorded_and_retry_counted(fake_client):
    fake_client.queue("[1] สวัสดี")
    fake_client.queue("[2] สวัสดี")
    result, _ = run(batch_of([(1, "Hello."), (2, "Goodbye.")]), fake_client)
    assert [entry.attempts for entry in result.entries] == [1, 2]


def test_results_come_back_in_batch_order(fake_client):
    fake_client.queue("[3] สาม\n[1] หนึ่ง\n[2] สอง")
    result, _ = run(batch_of([(1, "One."), (2, "Two."), (3, "Three.")]), fake_client)
    assert [entry.index for entry in result.entries] == [1, 2, 3]


# --- language validation ----------------------------------------------------


def test_chinese_leak_is_retried(fake_client):
    fake_client.queue("[1] 你好")
    fake_client.queue("[1] สวัสดี")
    result, _ = run(batch_of([(1, "Hello.")]), fake_client)
    assert fake_client.call_count == 2
    assert result.as_dict() == {1: "สวัสดี"}


def test_untagged_reply_is_retried_then_fails(fake_client):
    fake_client.queue("I cannot help with that.")
    fake_client.queue("Still no tags.")
    result, _ = run(batch_of([(1, "Hello.")]), fake_client, max_retries=1)
    assert result.failures[0].reason == "bad_response"


def test_han_in_the_source_is_allowed(fake_client):
    fake_client.queue("[1] ป้าย 欢迎")
    result, _ = run(batch_of([(1, "[SIGN: 欢迎] Come in.")]), fake_client)
    assert result.failures == []


def test_non_verbal_source_needs_no_thai(fake_client):
    fake_client.queue("[1] อืม")
    result, _ = run(batch_of([(1, "Mm-hmm.")]), fake_client)
    assert result.failures == []


def test_placeholder_loss_is_retried(fake_client):
    fake_client.queue("[1] สวัสดี")  # dropped {0} and {1}
    fake_client.queue("[1] {0}สวัสดี{1}")
    result, _ = run(batch_of([(1, "<i>Hello.</i>")]), fake_client)
    assert fake_client.call_count == 2
    assert result.as_dict() == {1: "{0}สวัสดี{1}"}


def test_a_truncated_merge_is_retried(fake_client):
    # Issue #19: a two-line cue came back holding only its first half.
    fake_client.queue("[1] ถ้ายิงเพื่อช่วยคน")
    fake_client.queue("[1] ถ้ายิงเพื่อช่วยคน\nคุณต้องเขียนคำขอโทษ")
    source = "If you shoot to save someone,\nyou have to write an apology."
    result, _ = run(batch_of([(1, source)]), fake_client)
    assert fake_client.call_count == 2
    assert result.failures == []
    assert result.as_dict()[1].count("\n") == 1


def test_a_second_short_reply_is_kept_not_left_in_english(fake_client):
    # A compact but complete merge can look like a truncation, so a repeat is
    # accepted: English would be the worse guess.
    fake_client.queue("[1] ถ้ายิงเพื่อช่วยคน")
    fake_client.queue("[1] ถ้ายิงเพื่อช่วยคน")
    source = "If you shoot to save someone,\nyou have to write an apology."
    result, _ = run(batch_of([(1, source)]), fake_client)
    assert fake_client.call_count == 2
    assert result.failures == []
    assert result.as_dict() == {1: "ถ้ายิงเพื่อช่วยคน"}


def test_a_short_reply_with_no_retries_left_is_kept(fake_client):
    fake_client.queue("[1] ถ้ายิงเพื่อช่วยคน")
    source = "If you shoot to save someone,\nyou have to write an apology."
    result, _ = run(batch_of([(1, source)]), fake_client, max_retries=0)
    assert fake_client.call_count == 1
    assert result.failures == []
    assert result.as_dict() == {1: "ถ้ายิงเพื่อช่วยคน"}


def test_a_line_final_full_stop_is_retried_then_kept(fake_client):
    # Issue #17: the stop is cosmetic, so a repeat is kept, never English.
    fake_client.queue("[1] ฉันไง.")
    fake_client.queue("[1] ฉันไง.")
    result, _ = run(batch_of([(1, "I am.")]), fake_client)
    assert fake_client.call_count == 2
    assert result.failures == []
    assert result.as_dict() == {1: "ฉันไง."}


def test_a_fixed_full_stop_is_accepted(fake_client):
    fake_client.queue("[1] ฉันไง.")
    fake_client.queue("[1] ฉันไง")
    result, _ = run(batch_of([(1, "I am.")]), fake_client)
    assert fake_client.call_count == 2
    assert result.as_dict() == {1: "ฉันไง"}


# --- giving up --------------------------------------------------------------


def test_exhausted_budget_keeps_english_and_flags(fake_client):
    for _ in range(4):
        fake_client.queue("[1] 你好")
    result, _ = run(batch_of([(1, "Hello.")]), fake_client)

    assert fake_client.call_count == 4  # first try + max_retries
    entry = result.entries[0]
    assert entry.ok is False
    assert entry.text == "Hello."  # English, timestamps intact elsewhere
    assert entry.reason in {"foreign_script", "missing_thai"}


def test_zero_retries_fails_after_one_call(fake_client):
    fake_client.queue("[1] 你好")
    result, _ = run(batch_of([(1, "Hello.")]), fake_client, max_retries=0)
    assert fake_client.call_count == 1
    assert result.failures[0].text == "Hello."


def test_failed_entries_are_reported_in_order(fake_client):
    for _ in range(2):
        fake_client.queue("[1] สวัสดี\n[2] 你好")
    result, _ = run(batch_of([(1, "Hello."), (2, "Goodbye.")]), fake_client, max_retries=1)
    assert [entry.index for entry in result.failures] == [2]


# --- transport --------------------------------------------------------------


def test_rate_limit_honours_retry_after(fake_client):
    fake_client.queue(llm.RateLimited("slow down", retry_after=7.5))
    fake_client.queue("[1] สวัสดี")
    result, recorder = run(batch_of([(1, "Hello.")]), fake_client)
    assert recorder.delays == [7.5]
    assert result.failures == []


def test_rate_limit_caps_absurd_retry_after(fake_client):
    fake_client.queue(llm.RateLimited("slow down", retry_after=9999))
    fake_client.queue("[1] สวัสดี")
    _, recorder = run(batch_of([(1, "Hello.")]), fake_client)
    assert recorder.delays == [120.0]


def test_rate_limit_without_header_uses_backoff(fake_client):
    fake_client.queue(llm.RateLimited("slow down"))
    fake_client.queue(llm.RateLimited("slow down"))
    fake_client.queue("[1] สวัสดี")
    _, recorder = run(batch_of([(1, "Hello.")]), fake_client)
    assert recorder.delays == [2.0, 4.0]


def test_timeout_backs_off_exponentially(fake_client):
    fake_client.queue(llm.TransportError("timeout"))
    fake_client.queue(llm.TransportError("timeout"))
    fake_client.queue("[1] สวัสดี")
    _, recorder = run(batch_of([(1, "Hello.")]), fake_client)
    assert recorder.delays == [2.0, 4.0]


def test_jitter_stays_within_bounds(fake_client):
    fake_client.queue(llm.TransportError("timeout"))
    fake_client.queue("[1] สวัสดี")
    _, recorder = run(
        batch_of([(1, "Hello.")]), fake_client, rand=lambda: 1.0, jitter=0.25
    )
    assert recorder.delays == [2.5]


def test_server_error_with_retry_after_is_honoured(fake_client):
    fake_client.queue(llm.TransportError("gateway", retry_after=3.0))
    fake_client.queue("[1] สวัสดี")
    result, recorder = run(batch_of([(1, "Hello.")]), fake_client)
    assert recorder.delays == [3.0]
    assert result.failures == []


def test_server_error_with_retry_after_is_still_capped(fake_client):
    fake_client.queue(llm.TransportError("gateway", retry_after=9999))
    fake_client.queue("[1] สวัสดี")
    _, recorder = run(batch_of([(1, "Hello.")]), fake_client)
    assert recorder.delays == [120.0]


@pytest.mark.parametrize("hint", [float("nan"), float("inf"), -5.0])
def test_unusable_retry_after_falls_back_to_backoff(fake_client, hint):
    fake_client.queue(llm.RateLimited("slow down", retry_after=hint))
    fake_client.queue("[1] สวัสดี")
    _, recorder = run(batch_of([(1, "Hello.")]), fake_client)
    assert recorder.delays == [2.0]


def test_zero_retry_after_retries_immediately(fake_client):
    fake_client.queue(llm.RateLimited("slow down", retry_after=0))
    fake_client.queue("[1] สวัสดี")
    _, recorder = run(batch_of([(1, "Hello.")]), fake_client)
    assert recorder.delays == [0.0]


def test_rate_limit_retries_are_counted_as_attempts(fake_client):
    fake_client.queue(llm.RateLimited("slow down", retry_after=1))
    fake_client.queue(llm.TransportError("timeout"))
    fake_client.queue("[1] สวัสดี")
    result, _ = run(batch_of([(1, "Hello.")]), fake_client)
    assert result.entries[0].attempts == 3


def test_rate_limit_and_timeout_share_one_budget(fake_client):
    fake_client.queue(llm.RateLimited("slow down", retry_after=1))
    fake_client.queue(llm.TransportError("timeout"))
    fake_client.queue(llm.RateLimited("slow down", retry_after=1))
    result, recorder = run(batch_of([(1, "Hello.")]), fake_client, max_retries=2)
    assert fake_client.call_count == 3
    assert len(recorder.delays) == 2
    assert result.failures[0].reason == "rate_limited"


def test_exhausted_transport_fails_with_transport_reason(fake_client):
    for _ in range(2):
        fake_client.queue(llm.TransportError("timeout"))
    result, _ = run(batch_of([(1, "Hello.")]), fake_client, max_retries=1)
    assert result.failures[0].reason == "transport"
    assert result.failures[0].text == "Hello."


def test_auth_error_is_not_retried(fake_client):
    fake_client.queue(llm.AuthError("bad key"))
    with pytest.raises(llm.AuthError):
        run(batch_of([(1, "Hello.")]), fake_client)
    assert fake_client.call_count == 1


# --- context ----------------------------------------------------------------


def test_extra_context_ids_are_ignored(fake_client):
    fake_client.queue("[0] Earlier.\n[1] สวัสดี\n[2] Later.")
    result, _ = run(batch_of([(1, "Hello.")]), fake_client)
    assert fake_client.call_count == 1
    assert result.as_dict() == {1: "สวัสดี"}


def test_context_is_resent_on_retry(fake_client):
    batch = batch_of([(2, "Hello.")], before=[ContextLine(1, "Earlier.")])
    fake_client.queue("[2] 你好")
    fake_client.queue("[2] สวัสดี")
    run(batch, fake_client)
    assert "[CONTEXT BEFORE - do not translate]\n[1] Earlier." in fake_client.calls[1][1]["content"]


def test_strict_is_passed_to_the_prompt(fake_client):
    fake_client.queue("[1] สวัสดี")
    run(batch_of([(1, "Hello.")]), fake_client, strict=True)
    assert "correction pass" in fake_client.calls[0][0]["content"]


def test_strict_is_off_by_default(fake_client):
    fake_client.queue("[1] สวัสดี")
    run(batch_of([(1, "Hello.")]), fake_client)
    assert "correction pass" not in fake_client.calls[0][0]["content"]


def test_current_translation_is_sent_to_the_model(fake_client):
    fake_client.queue("[1] สวัสดี")
    run(batch_of([(1, "Hello.")]), fake_client, current={1: "หวัดดี"})
    assert "[CURRENT TRANSLATIONS - flagged, improve or repeat]\n[1] หวัดดี" in (
        fake_client.calls[0][1]["content"]
    )
    assert "review pass" in fake_client.calls[0][0]["content"]
