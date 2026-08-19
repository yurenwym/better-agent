from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class AskValidationError(ValueError):
    """A model ask or user answer crossed the conversation boundary."""


ASK_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": "Ask the user for the minimum information needed to continue the current goal.",
        "parameters": {
            "type": "object",
            "required": ["questions"],
            "additionalProperties": False,
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "required": [
                            "id",
                            "header",
                            "question",
                            "options",
                            "multi_select",
                            "allow_free_text",
                        ],
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string", "minLength": 1, "maxLength": 64},
                            "header": {"type": "string", "minLength": 1, "maxLength": 80},
                            "question": {"type": "string", "minLength": 1, "maxLength": 400},
                            "options": {
                                "type": "array",
                                "minItems": 0,
                                "maxItems": 4,
                                "items": {
                                    "type": "object",
                                    "required": ["label", "description"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "label": {"type": "string", "minLength": 1, "maxLength": 80},
                                        "description": {"type": "string", "maxLength": 200},
                                    },
                                },
                            },
                            "multi_select": {"type": "boolean"},
                            "allow_free_text": {"type": "boolean"},
                        },
                    },
                }
            },
        },
    },
}


@dataclass(frozen=True)
class AskQuestion:
    id: str
    header: str
    question: str
    options: tuple[dict[str, str], ...]
    multi_select: bool
    allow_free_text: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "header": self.header,
            "question": self.question,
            "options": [dict(option) for option in self.options],
            "multi_select": self.multi_select,
            "allow_free_text": self.allow_free_text,
        }


@dataclass(frozen=True)
class AskRequest:
    call_id: str
    questions: tuple[AskQuestion, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "questions": [question.as_dict() for question in self.questions],
        }


def parse_ask_tool_call(call: dict[str, Any]) -> AskRequest:
    if not isinstance(call, dict):
        raise AskValidationError("ask tool call must be an object")
    call_id = call.get("id")
    function = call.get("function")
    if not isinstance(call_id, str) or not call_id.strip():
        raise AskValidationError("ask tool call id is required")
    if not isinstance(function, dict) or function.get("name") != "ask_user":
        raise AskValidationError("unsupported conversation tool")
    arguments = function.get("arguments", "{}")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise AskValidationError("ask tool arguments are invalid JSON") from exc
    if not isinstance(arguments, dict):
        raise AskValidationError("ask tool arguments must be an object")
    if set(arguments) != {"questions"}:
        raise AskValidationError("ask tool arguments contain unknown fields")
    questions = arguments.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
        raise AskValidationError("ask must contain between one and four questions")

    normalized: list[AskQuestion] = []
    seen_ids: set[str] = set()
    for raw in questions:
        normalized_question = _parse_question(raw, seen_ids)
        seen_ids.add(normalized_question.id)
        normalized.append(normalized_question)
    return AskRequest(call_id.strip(), tuple(normalized))


def normalize_answers(
    questions: tuple[AskQuestion, ...] | list[AskQuestion],
    answers: Any,
) -> list[dict[str, Any]]:
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise AskValidationError("one answer is required for each question")
    by_id = {question.id: question for question in questions}
    seen: set[str] = set()
    normalized_by_id: dict[str, dict[str, Any]] = {}
    for raw in answers:
        if not isinstance(raw, dict):
            raise AskValidationError("each answer must be an object")
        if set(raw) - {"question_id", "selected_options", "free_text"}:
            raise AskValidationError("answer contains unknown fields")
        question_id = raw.get("question_id")
        question = by_id.get(question_id) if isinstance(question_id, str) else None
        if question is None or question_id in seen:
            raise AskValidationError("answer question id is invalid or duplicated")
        seen.add(question_id)

        selected = raw.get("selected_options", [])
        if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
            raise AskValidationError("selected_options must be an array of strings")
        if len(set(selected)) != len(selected):
            raise AskValidationError("selected_options must not contain duplicates")
        allowed = {option["label"] for option in question.options}
        if any(item not in allowed for item in selected):
            raise AskValidationError("selected option is not available for this question")
        if not question.multi_select and len(selected) > 1:
            raise AskValidationError("single-choice question accepts one option")

        free_text = raw.get("free_text", "")
        if not isinstance(free_text, str):
            raise AskValidationError("free_text must be a string")
        free_text = free_text.strip()
        if free_text and not question.allow_free_text:
            raise AskValidationError("free text is not allowed for this question")
        if not selected and not free_text:
            raise AskValidationError("each question needs an answer")

        normalized_by_id[question_id] = {
            "question_id": question_id,
            "selected_options": list(selected),
            "free_text": free_text,
        }

    if seen != set(by_id):
        raise AskValidationError("an answer is missing")
    return [normalized_by_id[question.id] for question in questions]


def format_answer_message(
    questions: tuple[AskQuestion, ...] | list[AskQuestion],
    answers: list[dict[str, Any]],
) -> str:
    values = {answer["question_id"]: answer for answer in answers}
    lines = ["我补充了以下信息："]
    for question in questions:
        answer = values[question.id]
        parts = [*answer["selected_options"]]
        if answer["free_text"]:
            parts.append(answer["free_text"])
        lines.append(f"- {question.header}：{'、'.join(parts)}")
    return "\n".join(lines)


def tool_result_payload(
    questions: tuple[AskQuestion, ...] | list[AskQuestion],
    answers: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "questions": [question.as_dict() for question in questions],
        "answers": answers,
    }


def _parse_question(raw: Any, seen_ids: set[str]) -> AskQuestion:
    if not isinstance(raw, dict):
        raise AskValidationError("each ask question must be an object")
    required = {"id", "header", "question", "options", "multi_select", "allow_free_text"}
    if set(raw) != required:
        raise AskValidationError("ask question has missing or unknown fields")
    question_id = _text(raw["id"], "question id", 64)
    if question_id in seen_ids:
        raise AskValidationError("ask question ids must be unique")
    header = _text(raw["header"], "question header", 80)
    question = _text(raw["question"], "question text", 400)
    multi_select = raw["multi_select"]
    allow_free_text = raw["allow_free_text"]
    if not isinstance(multi_select, bool) or not isinstance(allow_free_text, bool):
        raise AskValidationError("ask selection flags must be boolean")
    options = raw["options"]
    if not isinstance(options, list) or len(options) > 4 or len(options) == 1:
        raise AskValidationError("ask options must contain zero or two to four choices")
    normalized_options: list[dict[str, str]] = []
    labels: set[str] = set()
    for option in options:
        if not isinstance(option, dict) or set(option) != {"label", "description"}:
            raise AskValidationError("ask option has missing or unknown fields")
        label = _text(option["label"], "option label", 80)
        description = option["description"]
        if not isinstance(description, str) or len(description.strip()) > 200:
            raise AskValidationError("option description is invalid")
        if label in labels:
            raise AskValidationError("option labels must be unique")
        labels.add(label)
        normalized_options.append({"label": label, "description": description.strip()})
    if not normalized_options and not allow_free_text:
        raise AskValidationError("a question needs choices or free text")
    return AskQuestion(question_id, header, question, tuple(normalized_options), multi_select, allow_free_text)


def _text(value: Any, label: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise AskValidationError(f"{label} must be a string")
    value = value.strip()
    if not value or len(value) > max_length:
        raise AskValidationError(f"{label} is invalid")
    return value
