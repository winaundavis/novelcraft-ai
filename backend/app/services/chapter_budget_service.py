"""Single, pre-call budget gate for one generated chapter."""

from __future__ import annotations

from dataclasses import dataclass


OPERATION_DIMENSIONS = {
    "generation": "generation", "retry": "retry",
    "fact_check": "qa", "fact_escalation": "qa", "hook_escalation": "qa", "conflict_analysis": "qa",
    "hook_check": "qa", "hook_resolution": "qa", "review": "qa",
    "fact_rewrite": "rewrite", "hook_rewrite": "rewrite",
    "style_rewrite": "rewrite", "ai_rewrite": "rewrite",
}


@dataclass(frozen=True)
class BudgetAllowance:
    operation: str
    dimension: str
    before: int
    after: int
    estimated_tokens: int


@dataclass
class ChapterGenerationBudget:
    max_generation_calls: int = 4
    max_retry_calls: int = 1
    max_qa_calls: int = 8
    max_rewrite_calls: int = 1
    target_words: int = 0
    acceptable_words: int = 0
    minimum_words: int = 0
    generation_calls: int = 0
    retry_calls: int = 0
    qa_calls: int = 0
    rewrite_calls: int = 0

    def consume(self, dimension: str, amount: int = 1) -> bool:
        current_name, maximum_name = f"{dimension}_calls", f"max_{dimension}_calls"
        if not hasattr(self, current_name) or not hasattr(self, maximum_name):
            raise ValueError(f"unknown chapter budget dimension: {dimension}")
        current, maximum = int(getattr(self, current_name)), int(getattr(self, maximum_name))
        if current + amount > maximum:
            return False
        setattr(self, current_name, current + amount)
        return True

    def allow_llm_call(self, operation: str, estimated_tokens: int = 0) -> BudgetAllowance | None:
        dimension = OPERATION_DIMENSIONS.get(operation)
        if not dimension:
            raise ValueError(f"unknown chapter budget operation: {operation}")
        before = int(getattr(self, f"{dimension}_calls"))
        if not self.consume(dimension):
            return None
        return BudgetAllowance(operation, dimension, before, before + 1, max(0, int(estimated_tokens)))

    def require(self, dimension: str, amount: int = 1) -> None:
        if not self.consume(dimension, amount):
            raise ChapterBudgetExceeded(dimension, self)

    def to_dict(self) -> dict[str, int]:
        return {key: int(value) for key, value in self.__dict__.items()}


class ChapterBudgetExceeded(RuntimeError):
    def __init__(self, dimension: str, budget: ChapterGenerationBudget) -> None:
        self.dimension, self.budget = dimension, budget
        super().__init__(f"chapter budget exceeded: {dimension}")
