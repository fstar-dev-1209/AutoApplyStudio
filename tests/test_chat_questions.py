"""Question prompts, fenced answers and cleaning (pure functions, no browser)."""

from __future__ import annotations

from core.chat_questions import (
    END,
    START,
    Question,
    build_context_prompt,
    build_question_prompt,
    clean_answer,
    extract_marked_answer,
    match_option,
)
from core.settings_store import AppSettings, normalize_proxy_url


def test_prompt_carries_the_question_the_style_and_the_rules():
    q = Question("Why do you want to work here?", max_chars=500)
    prompt = build_question_prompt(q, "Write like a human.")
    assert prompt.startswith("Write like a human.")
    assert "Why do you want to work here?" in prompt
    assert "at most 500 characters" in prompt
    assert START in prompt and END in prompt and "no markdown" in prompt


def test_choice_questions_list_the_options_verbatim():
    prompt = build_question_prompt(Question("Veteran status?", options=["Yes", "No", "I prefer not to say"], kind="choice"))
    assert '["Yes", "No", "I prefer not to say"]' in prompt


def test_context_prompt_has_job_and_resume():
    text = build_context_prompt(job_description="Build RAG.", resume_json={"name": "A"}, company="Acme", role="ML Eng")
    assert "ML Eng" in text and "Acme" in text and "Build RAG." in text and '"name": "A"' in text


def test_answer_is_lifted_from_between_the_markers():
    reply = f"Sure!\n{START}\nI led the migration.\n{END}\nAnything else?"
    assert extract_marked_answer(reply) == "I led the migration."


def test_the_last_markers_win_and_unfinished_or_placeholder_answers_are_rejected():
    assert extract_marked_answer(f"{START}old{END} {START}new{END}") == "new"
    assert extract_marked_answer(f"{START}half written") == ""
    assert extract_marked_answer("no markers at all") == ""
    assert extract_marked_answer(f"{START}...{END}") == ""
    assert extract_marked_answer(f"{START}   {END}") == ""


def test_cleaning_removes_markdown_and_machine_tells():
    raw = "**Built** the `search` stack — it served 2M users.\n\n\n\n- fast\n"
    out = clean_answer(raw)
    assert "**" not in out and "`" not in out and "—" not in out
    assert out.startswith("Built the search stack, it served 2M users.")
    assert "\n\n\n" not in out
    assert clean_answer('"Quoted answer"') == "Quoted answer"
    assert clean_answer("```\ncode fence\n```") == "code fence"


def test_the_length_limit_cuts_at_a_sentence_or_word_never_mid_word():
    text = "I built the ingestion service. It handled retries well. Then I added caching and observability everywhere."
    cut = clean_answer(text, max_chars=60)
    assert len(cut) <= 60 and cut.endswith(".")
    words = clean_answer("alpha beta gamma delta epsilon zeta", max_chars=14)
    assert words == "alpha beta" and len(words) <= 14


def test_match_option_is_exact_then_loose_and_refuses_ambiguity():
    options = ["Yes", "No", "I prefer not to say"]
    assert match_option("yes", options) == "Yes"
    assert match_option("  I PREFER   not to say ", options) == "I prefer not to say"
    assert match_option("maybe", options) == ""
    assert match_option("anything", []) == "anything"
    assert match_option("Yes or No", ["Yes", "No"]) == ""  # contains both: not a clear choice


def test_proxy_addresses_are_normalised():
    assert normalize_proxy_url("1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert normalize_proxy_url("socks5://user:pw@host:1080/path") == "socks5://host:1080"
    assert normalize_proxy_url("ftp://x") == "" and normalize_proxy_url("") == ""
    assert normalize_proxy_url("http://[::1]:3128") == "http://[::1]:3128"
    assert AppSettings.from_dict({"web_proxy": "host:3128"}).web_proxy == "http://host:3128"
    assert AppSettings.from_dict({"chat_provider": "bing"}).chat_provider == "claude"
    assert AppSettings.from_dict({"chat_provider": "chatgpt"}).chat_provider == "chatgpt"


def test_template_placeholders_in_a_generated_resume_are_found():
    from core.json_extract import find_placeholder

    assert find_placeholder({"experience": [{"role_title": "[Add verified entry-level or earlier job title]"}]}) \
        == "[Add verified entry-level or earlier job title]"
    assert find_placeholder({"a": ["fine", {"b": "Dates: [Insert verified dates]"}]}) == "[Insert verified dates]"
    # ordinary brackets in a real resume are not placeholders
    assert find_placeholder({"skills": ["Python [3.12]", "C# (.NET)", "[1] Led the migration"], "n": 3}) == ""
    assert find_placeholder("plain text") == "" and find_placeholder(None) == ""


def test_a_resume_with_a_placeholder_is_refused_and_one_without_is_accepted():
    import pytest

    from core.resume_finalize import finalize_resume_object
    from engine import ResumeError
    from tests.resume_fixtures import make_resume

    good = make_resume((2, 2))
    assert finalize_resume_object(dict(good), AppSettings())["name"] == "Test Person"
    bad = make_resume((2, 2))
    bad["experience"][0]["title"] = "[Add verified entry-level or earlier job title]"
    with pytest.raises(ResumeError, match="placeholder"):
        finalize_resume_object(bad, AppSettings())


def test_schema_descriptions_copied_from_the_prompt_are_not_values():
    from core.json_extract import find_placeholder

    assert "empty string" in find_placeholder({"company_name": "hiring company from JD, or empty string if absent"})
    assert find_placeholder({"company_name": "Acme", "summary": "Built the empty string handling for the parser."}) == ""


def test_the_marker_names_inside_the_question_are_never_taken_for_the_answer():
    from core.chat_questions import Question, build_question_prompt, extract_marked_answer
    prompt = build_question_prompt(Question("If employee referral, please provide their name."))
    assert extract_marked_answer(prompt) == ""
    assert extract_marked_answer(prompt + "\nChatGPT said:\n[[ANSWER_START]]N/A[[ANSWER_END]]") == "N/A"


def test_answers_are_counted_without_the_marker_names_in_the_question():
    from core.chat_questions import Question, build_question_prompt, count_marked_answers
    prompt = build_question_prompt(Question("Why us?"))
    assert count_marked_answers(prompt) == 0
    page = prompt + "\n[[ANSWER_START]]First[[ANSWER_END]]\n" + prompt + "\n[[ANSWER_START]]\nE) 9\n[[ANSWER_END]]"
    assert count_marked_answers(page) == 2


def test_work_history_goes_into_the_resume_prompt_and_foreign_employers_are_rejected():
    import pytest
    from core.prompt_builder import build_claude_chat_prompt
    from core.resume_finalize import ResumeError, employers_outside_history, finalize_resume_object
    from core.settings_store import AppSettings, ProfileSettings

    history = "Senior Engineer | Acme Corp | Austin, TX | Jan 2021 - Present"
    s = AppSettings(profile=ProfileSettings(work_history=history))
    assert history in build_claude_chat_prompt(s, job_description="Build things.")
    assert history not in build_claude_chat_prompt(AppSettings(), job_description="Build things.")

    ok = {"experience": [{"company": "ACME Corp.", "title": "Senior Engineer"}]}
    bad = {"experience": [{"company": "Acme Corp"}, {"company": "KPMG"}]}
    assert employers_outside_history(ok, history) == []
    assert employers_outside_history(bad, history) == ["KPMG"]
    assert employers_outside_history(bad, "") == []  # no history set: nothing to compare with
    with pytest.raises(ResumeError, match="not a truthful resume"):
        finalize_resume_object(dict(bad, name="x"), s)


def test_an_empty_message_box_is_not_a_paste_but_a_pasted_text_card_is():
    from ui.chat_service import ChatService
    holds = ChatService._box_holds_paste
    assert not holds({"composerLength": 1, "attachments": 0})      # an empty box reads as one blank line
    assert holds({"composerLength": 0, "attachments": 1})          # a long paste becomes a card, box text stays empty
    assert holds({"composerLength": 900, "attachments": 0})
    assert ChatService._composer_length({"composerLength": 0, "attachments": 2}) >= 10**6


def test_a_choice_is_matched_whatever_kind_of_dash_or_wording_the_form_and_the_chat_use():
    from core.chat_questions import match_option

    options = ["< 3 years", "3–5 years", ">5+ years"]  # the form uses a typographic en dash
    for answer in ("3-5 years", "3 - 5 years", "3–5 years", "3-5", "3 — 5 years"):
        assert match_option(answer, options) == "3–5 years", answer
    assert match_option("Less than 3 years", options) == "< 3 years"
    assert match_option("5+ years", options) == ">5+ years"
    assert match_option("4 years", options) == ""  # not one of them: never guessed


def test_the_chat_is_asked_once_more_when_its_answer_is_not_one_of_the_options():
    from core.form_answers import FILL, NEEDS, Context, Field, Option
    from core.greenhouse import GreenhouseApplier
    from core.settings_store import ProfileSettings

    asked = []

    def chat(question):
        asked.append(question)
        return "about four years" if len(asked) == 1 else "3–5 years"

    field = Field(ref="q", kind="combobox", label="Years with Terraform?", required=True,
                  options=[Option("< 3 years"), Option("3–5 years"), Option(">5+ years")])
    ctx = Context(profile=ProfileSettings(name="Mary Berg"), chat_enabled=True)
    decision = GreenhouseApplier(answer=chat)._ask(field, ctx)
    assert (decision.action, decision.value, decision.source) == (FILL, "3–5 years", "chat")
    assert len(asked) == 2 and "about four years" in asked[1].hint and "copied character for character" in asked[1].hint
    from core.chat_questions import build_question_prompt

    assert "about four years" in build_question_prompt(asked[1])

    # a chat that keeps answering outside the list ends as needs-you, with the first answer in the reason
    stubborn = GreenhouseApplier(answer=lambda q: "a few years")._ask(field, ctx)
    assert stubborn.action == NEEDS and "a few years" in stubborn.reason
    # an optional question it cannot answer is simply skipped
    optional = Field(ref="o", kind="combobox", label="Years?", required=False, options=field.options)
    assert GreenhouseApplier(answer=lambda q: "a few years")._ask(optional, ctx).action == "skip"


from core.chat_questions import salvage_unmarked  # noqa: E402


def test_facts_go_into_the_question_and_the_context():
    q = Question("Are you a U.S. citizen?", options=["Yes", "No"], kind="choice", facts="- Work status (text): US citizen")
    prompt = build_question_prompt(q)
    assert "MY PERSONAL FACTS" in prompt and "US citizen" in prompt and prompt.index("MY PERSONAL FACTS") < prompt.index("Question:")
    assert "MY PERSONAL FACTS" not in build_question_prompt(Question("Why us?"))
    ctx = build_context_prompt(job_description="jd", resume_json=None, company="A", role="B", facts="- Name: X")
    assert "- Name: X" in ctx


def test_a_reply_without_markers_is_used_when_it_names_exactly_one_option():
    asked = ("Question: Are you able to meet the requirements?\nReply with ONLY the answer between the markers "
             "[[ANSWER_START]] and [[ANSWER_END]], with nothing before or after them. Plain text only: no markdown, "
             "no mention of being an AI.\n")
    assert salvage_unmarked(asked + "Yes", ["Yes", "No"]) == "Yes"
    assert salvage_unmarked(asked + "I would say **No**, because...", ["Yes", "No"]) == "No"
    assert salvage_unmarked(asked + "Yes, or maybe no", ["Yes", "No"]) == ""  # two options: not settled
    assert salvage_unmarked(asked + "Yes - OPT", ["Yes", "Yes - OPT", "No"]) == "Yes - OPT"
    assert salvage_unmarked(asked, ["Yes", "No"]) == ""  # only our own question is on the page
    assert salvage_unmarked(asked + "about 7", [], "number") == "7"


def test_every_question_carries_the_facts_and_an_unmarked_reply_is_still_taken():
    from types import SimpleNamespace

    from core.apply_answers import make_answer_fn

    seen, logged = [], []

    def ask(prompt, *, chat_url, context_prompt, timeout_sec):
        seen.append((prompt, context_prompt))
        tail = prompt[-400:] + "\nNo."
        return SimpleNamespace(answer="", error="ChatGPT answered without the answer markers.", chat_url="", raw=tail)

    job = SimpleNamespace(job_description="jd", resume_json={}, company="A", role_title="B", chat_url="")
    answer = make_answer_fn(job, ask, facts_text="- Work status (text): US citizen", log=logged.append)
    got = answer(Question("Are you a citizen?", options=["Yes", "No"], kind="choice"))
    assert got == "No"
    assert "US citizen" in seen[0][0] and "US citizen" in seen[0][1]
    assert any("no markers" in m for m in logged)


from core.chat_questions import answer_after_prompt  # noqa: E402

_PROMPT = ('Write it by hand.\n\nQuestion: Are you okay showing your ID during the first Interview?\n'
           'Choose exactly one of these options and reply with it word for word: ["Yes", "No"]\n\n'
           'Reply with ONLY the answer between the markers [[ANSWER_START]] and [[ANSWER_END]], with nothing before or '
           'after them. Plain text only: no markdown, no mention of being an AI.')


def test_the_answer_after_our_question_is_found_even_when_the_same_word_was_said_before():
    older = "ChatGPT said:\n\n[[ANSWER_START]]Yes[[ANSWER_END]]\n\n"
    page = older + "You said:\n" + _PROMPT + "\n…\nShow more\nChatGPT said:\n\n[[ANSWER_START]]No[[ANSWER_END]]\n\nChatGPT can make mistakes."
    assert answer_after_prompt(page, _PROMPT) == "No"
    # our message is on the page but the reply has not started: the earlier answer is not ours
    assert answer_after_prompt(older + "You said:\n" + _PROMPT, _PROMPT) == ""


def test_a_long_question_shown_cut_short_is_still_found_by_its_question_line():
    page = ("ChatGPT said:\n\n[[ANSWER_START]]Yes[[ANSWER_END]]\n\nYou said:\nWrite it by hand. MY PERSONAL FACTS ... "
            "Question: Are you okay showing your ID during the first Interview?\n…\nShow more\n"
            "ChatGPT said:\n\n[[ANSWER_START]]No[[ANSWER_END]]")
    assert answer_after_prompt(page, _PROMPT) == "No"


def test_an_earlier_question_and_its_answer_are_never_taken_for_ours():
    earlier = _PROMPT.replace("Are you okay showing your ID during the first Interview?", "Can you work weekends?")
    page = "You said:\n" + earlier + "\nChatGPT said:\n\n[[ANSWER_START]]Yes[[ANSWER_END]]\n"
    assert answer_after_prompt(page, _PROMPT) == ""  # our question has not appeared yet


def test_a_reply_the_reader_missed_is_still_taken_from_the_end_of_the_page():
    raw = (_PROMPT[-300:] + "\n…\nShow more\nChatGPT said:\n\n[[ANSWER_START]]Yes[[ANSWER_END]]\n\nChatGPT can make mistakes.")
    assert salvage_unmarked(raw, ["Yes", "No"]) == "Yes"
    assert salvage_unmarked(raw, ["Maybe", "No"]) == ""  # an answer that is not an option is not taken
