from pathlib import Path

from app.config import PROMPTS_DIR

_OPS = "ops_playbook"
_WITH_OPS = {
    "sentiment",
    "technicals",
    "fundamentals",
    "strategist",
    "risk",
    "desk",
    "executor",
    "book",
    "run_brief",
    "floor",
}


def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    body = path.read_text(encoding="utf-8") if path.exists() else f"You are agent {name}. Follow the user's instructions carefully."
    if name in _WITH_OPS and name != _OPS:
        ops = PROMPTS_DIR / f"{_OPS}.md"
        if ops.exists():
            body = body + "\n\n---\n" + ops.read_text(encoding="utf-8")
    return body
