import hashlib
import json
from dataclasses import astuple, dataclass

from app.llm import LLM
from app.metacritic import Review
from app.models import ReviewSource

MAX_POINTS = 5
MAX_QUOTE_CHARS = 500
# shortcut: fixed prompt budget that fits Ollama's default 4k context, raise it for hosted models
MAX_PROMPT_CHARS = 8000

SYSTEM_PROMPT = f"""You summarize video game reviews for a game catalog.
Reply with a JSON object only: {{"likes": [...], "dislikes": [...]}}.
"likes" lists what reviewers praise, "dislikes" what they criticize.
Each list has at most {MAX_POINTS} short points in English, based only on the reviews given.
Merge points that say the same thing. Use an empty list when the reviews give nothing for it."""


@dataclass(frozen=True)
class Summary:
    likes: list[str]
    dislikes: list[str]


def reviews_hash(reviews: list[Review]) -> str:
    rows = sorted(json.dumps(astuple(review)) for review in reviews)
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def build_prompt(title: str, source: ReviewSource, reviews: list[Review]) -> str:
    lines: list[str] = []
    size = 0
    for review in reviews:
        score = "unscored" if review.score is None else f"score {review.score:g}"
        line = f"- ({score}) {review.quote[:MAX_QUOTE_CHARS]}"
        size += len(line) + 1
        if size > MAX_PROMPT_CHARS:
            break
        lines.append(line)
    return f"Game: {title}\n{source.capitalize()} reviews:\n" + "\n".join(lines)


def parse_summary(data: dict[str, object]) -> Summary:
    def points(key: str) -> list[str]:
        value = data.get(key) or []
        if not isinstance(value, list):
            raise ValueError(f"LLM summary field {key!r} is not a list: {value!r}")
        return [text for item in value if (text := str(item).strip())][:MAX_POINTS]

    return Summary(likes=points("likes"), dislikes=points("dislikes"))


def summarize(llm: LLM, title: str, source: ReviewSource, reviews: list[Review]) -> Summary:
    return parse_summary(llm.complete_json(SYSTEM_PROMPT, build_prompt(title, source, reviews)))
