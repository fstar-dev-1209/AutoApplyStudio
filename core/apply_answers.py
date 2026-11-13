"""Answering an application form's open questions through the job's own chat conversation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from core.chat_questions import (
    DEFAULT_ANSWER_PROMPT,
    Question,
    build_context_prompt,
    build_question_prompt,
    extract_marked_answer,
)
from core.models import JobItem


class AskResultLike(Protocol):
    answer: str
    error: str
    chat_url: str
    raw: str


AskChat = Callable[..., AskResultLike]  # MainWindow.ask_chat


class ChatUnavailable(RuntimeError):
    pass


def make_answer_fn(
    job: JobItem,
    ask_chat: AskChat,
    *,
    style_prompt: str = "",
    on_conversation: Callable[[str], None] = lambda url: None,
    timeout_sec: int = 300,
) -> Callable[[Question], str]:
    """A function that answers one form question in the chat that wrote this job's resume.

    The saved conversation is used when there is one; otherwise (or if it is gone) a new chat is started with the job
    description and the resume JSON first. A reply that arrives without the answer markers is asked for once more.
    """
    context = build_context_prompt(job_description=job.job_description, resume_json=job.resume_json,
                                   company=job.company, role=job.role_title)
    state = {"url": job.chat_url}

    def answer(question: Question) -> str:
        prompt = build_question_prompt(question, style_prompt.strip() or DEFAULT_ANSWER_PROMPT)
        last_error = "no answer"
        for attempt in range(2):
            extra = "" if attempt == 0 else (
                "\n\nYour previous reply did not follow the format. Reply again with ONLY the answer between the "
                "markers [[ANSWER_START]] and [[ANSWER_END]].")
            result = ask_chat(prompt + extra, chat_url=state["url"], context_prompt=context, timeout_sec=timeout_sec)
            if result.chat_url and result.chat_url != state["url"]:
                state["url"] = result.chat_url
                on_conversation(result.chat_url)  # the conversation to reuse for the next question
            if not result.error and result.answer:
                return result.answer
            last_error = result.error or "empty answer"
            if "markers" not in last_error:
                break  # a sign-in or timeout problem: asking again will not help
        raise ChatUnavailable(last_error)

    return answer


__all__ = ["ChatUnavailable", "extract_marked_answer", "make_answer_fn"]
