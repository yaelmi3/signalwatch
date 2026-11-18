from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, StringConstraints

type TeamInterest = Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)]


class TeamInterests(BaseModel):
    marketing: TeamInterest = ""
    product: TeamInterest = ""
    research_and_development: TeamInterest = ""
    business_context: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ""


class AnalysisQuality(StrEnum):
    ECONOMY = "economy"
    HIGHER_QUALITY = "higher_quality"


class AnalysisSettings(BaseModel):
    quality: AnalysisQuality


class TeamDecision(BaseModel):
    relevant: bool
    reason: str
    quote: str | None


class ChangeAnalysisResult(BaseModel):
    summary: str
    marketing: TeamDecision
    product: TeamDecision
    research_and_development: TeamDecision


class PageChangeAnalysis(BaseModel):
    status: Literal["queued", "completed", "failed"]
    result: ChangeAnalysisResult | None
