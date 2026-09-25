"""Summarize an extracted schema so a survey config can be written against it.

Authoring a config means answering two questions about a form you have just
extracted: *what response formats does it use* and *which items measure what*.
The first is mechanical — group the questions by their option signature — so
this command does it for you and emits a ready-to-paste ``scales:`` block.

The second is a judgement about your instrument and stays with you. What this
gives you is the item inventory to make it against.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from personabot.domain.models import FormSchema, Question, QuestionType


def _yaml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _slug(index: int, size: int) -> str:
    return f"scale_{index}_k{size}"


def _fit(text: str, width: int | None) -> str:
    """Collapse whitespace, truncating only when a width is given.

    ``--full`` passes ``None``. The long items are exactly the ones that need
    judgement when they are assigned to constructs, so an inventory meant to be
    read against the form — rather than skimmed in a terminal — must not cut
    them off mid-sentence.
    """
    text = " ".join(text.split())
    if width is not None and len(text) > width:
        text = text[: width - 1] + "\u2026"
    return text


@dataclass(slots=True)
class SignatureGroup:
    options: tuple[str, ...]
    questions: list[Question] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.options)


@dataclass(slots=True)
class SchemaSummary:
    schema: FormSchema
    groups: list[SignatureGroup]
    free_text: list[Question]

    def render(self, *, text_width: int | None = 72) -> str:
        types = Counter(str(q.type) for q in self.schema)
        lines = [
            "",
            "=" * 78,
            f"SCHEMA INVENTORY — {len(self.schema)} questions",
            "=" * 78,
            "  types: " + ", ".join(f"{name}={count}" for name, count in sorted(types.items())),
            f"  distinct option signatures: {len(self.groups)}",
            f"  free-text questions: {len(self.free_text)}",
        ]

        for index, group in enumerate(self.groups, start=1):
            lines += [
                "",
                "-" * 78,
                f"SIGNATURE {index} — K={group.size}, {len(group.questions)} question(s)",
                "-" * 78,
                "  options (in order):",
            ]
            lines += [f"    [{i}] {option}" for i, option in enumerate(group.options)]
            lines.append("  questions:")
            for question in group.questions:
                lines.append(
                    f"    {question.question_id}  "
                    f"{_fit(question.question_text, text_width)}"
                )

        if self.free_text:
            lines += ["", "-" * 78, "FREE TEXT / NO OPTIONS", "-" * 78]
            for question in self.free_text:
                lines.append(
                    f"    {question.question_id}  "
                    f"{_fit(question.question_text, text_width)}"
                )
            lines.append("")
            lines.append(
                "  These usually become `static_answers` bound to persona fields,"
            )
            lines.append(
                "  or `skip` entries. A free-text item left in a construct is sent"
            )
            lines.append("  to the model as an open question.")

        lines += ["", "=" * 78, "DRAFT `scales:` BLOCK — paste into your survey YAML", "=" * 78]
        lines.append(self.render_scales())
        lines += [
            "",
            "Rename each scale to something meaningful, and check `coding`:",
            "  low_first  = option [0] is the LOW end  (score = index + 1)",
            "  high_first = option [0] is the HIGH end (score = K - index)",
            "The draft assumes low_first. Look at each list above and fix any that",
            "run high to low — getting this wrong deflates that construct's alpha.",
            "",
        ]
        return "\n".join(lines)

    def render_scales(self) -> str:
        lines = ["scales:"]
        for index, group in enumerate(self.groups, start=1):
            lines.append(f"  - name: {_slug(index, group.size)}")
            lines.append("    match:")
            lines.append("      options_equal:")
            lines += [f"        - {_yaml_quote(option)}" for option in group.options]
            lines.append("    coding: low_first")
        return "\n".join(lines)

    def render_id_checklist(self) -> str:
        """Every choice question id, for building `question_id_in` lists."""
        lines = []
        for index, group in enumerate(self.groups, start=1):
            ids = ", ".join(f'"{q.question_id}"' for q in group.questions)
            lines.append(f"# signature {index} (K={group.size}): [{ids}]")
        return "\n".join(lines)


def summarize(schema: FormSchema) -> SchemaSummary:
    buckets: dict[tuple[str, ...], list[Question]] = defaultdict(list)
    free_text: list[Question] = []

    for question in schema:
        if question.is_choice:
            buckets[question.option_values].append(question)
        else:
            free_text.append(question)

    # Largest groups first: those are the psychometric scales, and the one- or
    # two-item groups are usually demographics.
    groups = [
        SignatureGroup(options=options, questions=questions)
        for options, questions in sorted(
            buckets.items(), key=lambda item: (-len(item[1]), -len(item[0]))
        )
    ]
    return SchemaSummary(schema=schema, groups=groups, free_text=free_text)
