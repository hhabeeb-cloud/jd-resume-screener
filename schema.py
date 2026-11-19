"""Validated, provider-compatible contracts for resume evaluations."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CandidateEvaluation(BaseModel):
    """Evidence-based assessment of a single resume against a job description."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    candidate_name: str = Field(min_length=1, max_length=300)
    overall_fit_score: int = Field(ge=0, le=100)
    decision: Literal["Shortlist", "Review", "Reject"]
    executive_summary: str = Field(min_length=1, max_length=4000)
    matched_skills: list[str] = Field(description="Required skills evidenced in the resume.")
    missing_critical_skills: list[str] = Field(
        description="Hard JD requirements missing or not verifiable from the resume."
    )
    transferable_skills: list[str]
    experience_verdict: str = Field(min_length=1, max_length=4000)
    tailored_interview_questions: list[str] = Field(min_length=3, max_length=4)

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        """Enforce business rules even when the model returns valid-looking JSON."""
        expected = (
            "Shortlist" if self.overall_fit_score >= 75
            else "Review" if self.overall_fit_score >= 55
            else "Reject"
        )
        if self.decision != expected:
            raise ValueError(f"Score {self.overall_fit_score} requires decision {expected}.")

        # JSON-schema array limits do not prohibit blank strings within the arrays.
        for field_name in (
            "matched_skills", "missing_critical_skills", "transferable_skills",
            "tailored_interview_questions",
        ):
            if any(not entry.strip() for entry in getattr(self, field_name)):
                raise ValueError(f"{field_name} must not contain blank entries.")
        return self