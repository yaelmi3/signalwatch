"""Compare one team interest on saved article evidence."""

import sys
from typing import Literal

import structlog
from pydantic import BaseModel, Field, ValidationError

from signalwatch.analysis_models import TeamInterest, TeamInterests
from signalwatch.analysis_worker import _TransientModelError, analyze_article
from signalwatch.storage import DigestTeam


class ArticleCase(BaseModel):
    title: str = Field(min_length=1)
    publisher_text: str = Field(min_length=1, max_length=12_000)
    expected: Literal["show", "hold"]


class Replay(BaseModel):
    model: str = Field(min_length=1)
    team: DigestTeam
    interests: TeamInterests
    proposed_interest: TeamInterest
    articles: list[ArticleCase] = Field(min_length=1)


def compare(replay: Replay) -> int:
    proposed = replay.interests.model_copy(update={replay.team.value: replay.proposed_interest})
    print(f"Team {replay.team.value} · Model {replay.model}")
    incomplete = False
    for article in replay.articles:
        logger = structlog.get_logger().bind(article=article.title)
        print(f"{article.title} · Expected {article.expected}")
        for label, interests in (("Current", replay.interests), ("Proposed", proposed)):
            try:
                result = analyze_article(article.title, article.publisher_text, interests, replay.model, logger)
            except _TransientModelError as error:
                print(f"  {label} · {error}")
                incomplete = True
                continue
            if result is None:
                print(f"  {label} · no completed analysis")
                incomplete = True
                continue
            decision = getattr(result, replay.team.value)
            outcome = "show" if result.change_quote and decision.relevant else "hold"
            print(f"  {label} · {outcome} · {decision.reason}")
    return int(incomplete)


def main() -> int:
    try:
        replay = Replay.model_validate_json(sys.stdin.read())
    except ValidationError as error:
        print(error, file=sys.stderr)
        return 2
    return compare(replay)


if __name__ == "__main__":
    raise SystemExit(main())
