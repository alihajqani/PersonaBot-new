"""Psychometric validation of the generated corpus.

Four audit findings are fixed here. Each was a way the old harness could not
report a problem even when one existed.

**A3 — no more auto-reversal.** The predecessor reflected any item whose
corrected item-total correlation came out negative, then computed alpha. With
modest *n*, an item whose true correlation is near zero flips sign by chance;
reflecting it guarantees a positive contribution, so alpha is **systematically
inflated** — the exact artifact this project exists to detect. Worse, it
re-derived reverse-keying independently of the instrument, so the two could
disagree silently. The config is now the single source of truth, and a
disagreement is *reported* rather than silently applied.

**A4 — response-style indices skipped demographics.** The old code iterated the
whole schema. For a two-option item, ``c == 1 or c == K`` is always true, so
every gender or income answer counted as an extreme response.

**A5 — straightlining is measured within a construct.** Pooling all items mixes
scales of different length and direction, so a genuine straightliner varies
across scales and escapes the threshold.

**A6 — the midpoint rate divides by odd-length items only.** Even-K items can
never contain a midpoint, so including them deflated the rate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from personabot.domain.instrument import Instrument
from personabot.infrastructure.storage.json_store import read_json_or_none, write_json
from personabot.logging_setup import get_logger
from personabot.workspace import Workspace

log = get_logger(__name__)


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def variance(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return sum((v - m) ** 2 for v in values) / (len(values) - 1)


def stdev(values: Sequence[float]) -> float:
    return math.sqrt(variance(values))


def correlation(xs: Sequence[float], ys: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mx, my = mean(xs), mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else 0.0


@dataclass(slots=True)
class ItemStats:
    question_id: str
    mean: float
    sd: float
    item_total_r: float
    reverse_keyed: bool
    sign_disagrees: bool


@dataclass(slots=True)
class ConstructStats:
    key: str
    label: str
    n_items: int
    n_complete: int
    alpha: float | None = None
    mean_inter_item_r: float | None = None
    scale_mean: float | None = None
    scale_sd: float | None = None
    items: list[ItemStats] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AnalysisReport:
    n_respondents: int
    constructs: list[ConstructStats] = field(default_factory=list)
    response_styles: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_respondents": self.n_respondents,
            "constructs": [
                {
                    "key": c.key, "label": c.label, "n_items": c.n_items,
                    "n_complete": c.n_complete, "alpha": c.alpha,
                    "mean_inter_item_r": c.mean_inter_item_r,
                    "scale_mean": c.scale_mean, "scale_sd": c.scale_sd,
                    "notes": c.notes,
                    "items": [
                        {
                            "id": i.question_id, "mean": i.mean, "sd": i.sd,
                            "item_total_r": i.item_total_r,
                            "reverse_keyed": i.reverse_keyed,
                            "sign_disagrees": i.sign_disagrees,
                        }
                        for i in c.items
                    ],
                }
                for c in self.constructs
            ],
            "response_styles": self.response_styles,
            "warnings": self.warnings,
        }

    def render(self) -> str:
        lines = ["", "=" * 66,
                 f"PSYCHOMETRIC REPORT — {self.n_respondents} synthetic respondents",
                 "=" * 66]

        for construct in self.constructs:
            lines.append(f"\n■ {construct.label} ({construct.key})")
            lines.append(f"    items {construct.n_items} | complete respondents {construct.n_complete}")
            if construct.alpha is not None:
                lines.append(f"    Cronbach alpha        : {construct.alpha:.3f}")
            if construct.mean_inter_item_r is not None:
                lines.append(f"    mean inter-item r     : {construct.mean_inter_item_r:.3f}")
            if construct.scale_mean is not None:
                lines.append(
                    f"    scale mean (1..K)     : {construct.scale_mean:.2f} "
                    f"(sd {construct.scale_sd:.2f})"
                )
            disagreeing = [i.question_id for i in construct.items if i.sign_disagrees]
            if disagreeing:
                lines.append(
                    f"    ! item-total r sign disagrees with the config's reverse "
                    f"keying for: {disagreeing}"
                )
            for note in construct.notes:
                lines.append(f"    ! {note}")

        if self.response_styles:
            lines.append("\n■ Response styles")
            for key, value in self.response_styles.items():
                shown = f"{value:.3f}" if isinstance(value, float) else value
                lines.append(f"    {key:24s}: {shown}")

        if self.warnings:
            lines.append("\n■ Warnings")
            lines += [f"    ! {w}" for w in self.warnings]

        lines.append("")
        return "\n".join(lines)


def load_responses(workspace: Workspace) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for directory in (workspace.answers_dir, workspace.answers_dir / "done"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            payload = read_json_or_none(path)
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _coded_matrix(
    instrument: Instrument, question_ids: Sequence[str], rows: Iterable[dict[str, Any]]
) -> list[list[float]]:
    """Listwise-complete numeric matrix for one construct."""
    matrix: list[list[float]] = []
    for row in rows:
        coded: list[float] = []
        for question_id in question_ids:
            value = row.get(question_id)
            code = (
                instrument.numeric_code(instrument.schema[question_id], str(value))
                if value is not None else None
            )
            if code is None:
                break
            coded.append(float(code))
        if len(coded) == len(question_ids):
            matrix.append(coded)
    return matrix


def analyze_construct(
    instrument: Instrument, construct, questions, rows
) -> ConstructStats:
    question_ids = [q.question_id for q in questions]
    stats = ConstructStats(
        key=construct.key, label=construct.label,
        n_items=len(questions), n_complete=0,
    )

    matrix = _coded_matrix(instrument, question_ids, rows)
    stats.n_complete = len(matrix)
    if len(matrix) < 3:
        stats.notes.append("too few complete respondents to analyse")
        return stats

    config = instrument.spec.analysis
    columns = [[row[j] for row in matrix] for j in range(len(question_ids))]

    # Reverse-key from the CONFIG, never from the observed sign (A3).
    reversed_flags = [instrument.is_reverse(qid) for qid in question_ids]
    scored = [
        [(1 + instrument.schema[question_ids[j]].scale_size) - v for v in columns[j]]
        if reversed_flags[j] else list(columns[j])
        for j in range(len(question_ids))
    ]

    totals = [sum(scored[j][i] for j in range(len(question_ids))) for i in range(len(matrix))]

    item_stats: list[ItemStats] = []
    for j, question_id in enumerate(question_ids):
        rest = [totals[i] - scored[j][i] for i in range(len(matrix))]
        itc = correlation(scored[j], rest)
        item_stats.append(
            ItemStats(
                question_id=question_id,
                mean=round(mean(scored[j]), 2),
                sd=round(stdev(scored[j]), 2),
                item_total_r=round(itc, 3),
                reverse_keyed=reversed_flags[j],
                # Reported, not acted on. A negative correlation after applying
                # the declared keying means the config and the data disagree —
                # information worth having, not something to paper over.
                sign_disagrees=bool(config.warn_on_sign_disagreement and itc < 0),
            )
        )
    stats.items = item_stats

    k = len(question_ids)
    if k >= config.min_items_for_alpha:
        total_variance = variance(totals)
        if total_variance > 0:
            item_variance_sum = sum(variance(column) for column in scored)
            stats.alpha = round(
                (k / (k - 1)) * (1 - item_variance_sum / total_variance), 3
            )
    else:
        stats.notes.append(
            f"only {k} items — alpha is unstable below {config.min_items_for_alpha} "
            f"and is better read as a correlation"
        )

    inter = [
        correlation(scored[a], scored[b])
        for a in range(k) for b in range(a + 1, k)
    ]
    if inter:
        stats.mean_inter_item_r = round(mean(inter), 3)

    scale_scores = [total / k for total in totals]
    stats.scale_mean = round(mean(scale_scores), 3)
    stats.scale_sd = round(stdev(scale_scores), 3)

    if stats.alpha is not None and stats.alpha > config.sanity.alpha_max:
        stats.notes.append(
            f"alpha {stats.alpha:.3f} exceeds {config.sanity.alpha_max} — implausibly "
            f"high internal consistency, the signature of LLM self-consistency"
        )
    if (stats.mean_inter_item_r or 0) > config.sanity.mean_inter_item_r_max:
        stats.notes.append(
            f"mean inter-item r {stats.mean_inter_item_r:.3f} exceeds "
            f"{config.sanity.mean_inter_item_r_max} — over-clean correlation structure"
        )

    return stats


def analyze_response_styles(instrument: Instrument, rows) -> dict[str, Any]:
    config = instrument.spec.analysis
    minimum_points = config.response_style_min_scale_points

    extreme_rates, acquiescence_rates, midpoint_rates = [], [], []
    straightliners = 0
    counted = 0

    constructs = instrument.grouped_by_construct()

    for row in rows:
        codes: list[float] = []
        endpoints = agree = odd_items = midpoints = 0
        per_construct: dict[str, list[float]] = {}

        for construct, questions in constructs:
            for question in questions:
                # A4: two-option items always look "extreme"; exclude them.
                if question.scale_size < minimum_points:
                    continue
                value = row.get(question.question_id)
                code = (
                    instrument.numeric_code(question, str(value))
                    if value is not None else None
                )
                if code is None:
                    continue

                size = question.scale_size
                codes.append(float(code))
                per_construct.setdefault(construct.key, []).append(float(code))

                if code == 1 or code == size:
                    endpoints += 1
                if code >= (size / 2) + 0.5:
                    agree += 1
                if size % 2 == 1:
                    odd_items += 1
                    if code == (size + 1) / 2:
                        midpoints += 1

        if len(codes) < 5:
            continue
        counted += 1

        extreme_rates.append(endpoints / len(codes))
        acquiescence_rates.append(agree / len(codes))
        # A6: only odd-length scales have a midpoint at all.
        if odd_items:
            midpoint_rates.append(midpoints / odd_items)

        # A5: flatness is a within-construct property.
        if config.straightlining.scope == "within_construct":
            flat = [
                stdev(values) < config.straightlining.sd_threshold
                for values in per_construct.values()
                if len(values) >= 3
            ]
            if flat and all(flat):
                straightliners += 1
        elif stdev(codes) < config.straightlining.sd_threshold:
            straightliners += 1

    return {
        "n_respondents": counted,
        "extreme_response_rate": round(mean(extreme_rates), 3) if extreme_rates else None,
        "acquiescence_rate": round(mean(acquiescence_rates), 3) if acquiescence_rates else None,
        "midpoint_rate": round(mean(midpoint_rates), 3) if midpoint_rates else None,
        "straightlining_rate": round(straightliners / counted, 3) if counted else None,
    }


def run(workspace: Workspace, instrument: Instrument) -> AnalysisReport:
    rows = load_responses(workspace)
    report = AnalysisReport(n_respondents=len(rows))

    if not rows:
        report.warnings.append("No answer files found. Generate answers first.")
        return report

    for construct, questions in instrument.grouped_by_construct():
        report.constructs.append(
            analyze_construct(instrument, construct, questions, rows)
        )

    report.response_styles = analyze_response_styles(instrument, rows)

    if instrument.spec.analysis.reverse_keys_from != "config":
        report.warnings.append(
            "reverse_keys_from is not 'config'. Deriving reverse keys from the data "
            "capitalizes on chance and inflates alpha."
        )

    write_json(workspace.report_file, report.to_dict())
    return report
