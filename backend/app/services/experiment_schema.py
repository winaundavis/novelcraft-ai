"""Data-only schemas for a future, explicitly approved comparison experiment."""

from __future__ import annotations

from dataclasses import dataclass, field


REVIEW_DIMENSIONS = (
    "continuity", "plot_progression", "character_voice", "emotion", "pacing",
    "style_adherence", "readability", "web_novel_pull", "overall_preference",
    "state_consistency", "memory_usefulness", "character_arc_coherence",
)


@dataclass(frozen=True)
class HumanReview:
    continuity: int
    plot_progression: int
    character_voice: int
    emotion: int
    pacing: int
    style_adherence: int
    readability: int
    web_novel_pull: int
    overall_preference: int = 3
    state_consistency: int = 3
    memory_usefulness: int = 3
    character_arc_coherence: int = 3
    reason: str = ""

    def __post_init__(self) -> None:
        if any(not 1 <= getattr(self, name) <= 5 for name in REVIEW_DIMENSIONS):
            raise ValueError("human review scores must be in the range 1..5")


@dataclass(frozen=True)
class ComparisonDesign:
    baseline_name: str
    candidate_name: str
    single_changed_variable: str
    same_subject: bool = True
    same_outline: bool = True
    same_characters: bool = True
    same_target_words: bool = True
    same_model: bool = True


@dataclass(frozen=True)
class Experiment001:
    id: str = "Experiment-001"
    chapters: int = 3
    target_output_words: tuple[int, int] = (3000, 5000)
    max_usd: float = 10.0
    status: str = "prepared_do_not_run"
    estimated_calls: int | None = None
    estimated_tokens: int | None = None
    estimated_cost_usd: float | None = None
    design: ComparisonDesign = field(default_factory=lambda: ComparisonDesign(
        baseline_name="direct model prompt", candidate_name="NovelCraft harness",
        single_changed_variable="NovelCraft context and contract harness",
    ))

    def may_run(self) -> bool:
        return self.status == "approved" and self.estimated_cost_usd is not None and self.estimated_cost_usd <= self.max_usd
