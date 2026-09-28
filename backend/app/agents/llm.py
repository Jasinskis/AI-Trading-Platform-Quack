from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.config import get_settings
from app.services.cost import BudgetExceeded, cost_tracker


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", text)
        if m:
            return json.loads(m.group(0))
        raise


def _sync_messages_create(
    *,
    api_key: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout: float,
) -> Any:
    """Run in a worker thread so the asyncio event loop stays responsive."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=1)
    return client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )


async def _record(msg: Any) -> None:
    usage = getattr(msg, "usage", None)
    if not usage:
        return
    await cost_tracker.record_usage(
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


async def claude_json(
    system: str,
    user: str,
    *,
    max_tokens: int = 4096,
    timeout: float = 75.0,
) -> Any:
    settings = get_settings()
    if settings.mock_llm or not settings.anthropic_api_key:
        return {"mock": True, "note": "MOCK_LLM or missing ANTHROPIC_API_KEY", "user_preview": user[:200]}

    cost_tracker.ensure_allowed()
    sys_msg = system + "\n\nAlways respond with valid JSON only. No markdown. Keep answers compact."

    async def _once(u: str) -> Any:
        msg = await asyncio.wait_for(
            asyncio.to_thread(
                _sync_messages_create,
                api_key=settings.anthropic_api_key,
                model=settings.anthropic_model,
                system=sys_msg,
                user=u,
                max_tokens=max_tokens,
                timeout=max(5.0, timeout - 5.0),
            ),
            timeout=timeout,
        )
        await _record(msg)
        text = "".join(b.text for b in msg.content if hasattr(b, "text"))
        return _extract_json(text)

    try:
        return await _once(user)
    except asyncio.TimeoutError as e:
        raise TimeoutError(f"Claude JSON timed out after {timeout:.0f}s") from e
    except (json.JSONDecodeError, ValueError):
        # One repair pass — common when output is truncated mid-JSON
        repair = (
            user
            + "\n\nPREVIOUS OUTPUT WAS INVALID JSON. Reply again with COMPLETE valid JSON only, "
            "smaller if needed, no markdown."
        )
        try:
            return await _once(repair)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"Claude JSON timed out after {timeout:.0f}s") from e
        except (json.JSONDecodeError, ValueError):
            raise


async def claude_text(
    system: str,
    user: str,
    *,
    max_tokens: int = 2048,
    timeout: float = 45.0,
) -> str:
    settings = get_settings()
    if settings.mock_llm or not settings.anthropic_api_key:
        return f"[mock] {user[:300]}"

    cost_tracker.ensure_allowed()
    try:
        msg = await asyncio.wait_for(
            asyncio.to_thread(
                _sync_messages_create,
                api_key=settings.anthropic_api_key,
                model=settings.anthropic_model,
                system=system,
                user=user,
                max_tokens=max_tokens,
                timeout=max(5.0, timeout - 5.0),
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        raise TimeoutError(f"Claude text timed out after {timeout:.0f}s") from e
    await _record(msg)
    return "".join(b.text for b in msg.content if hasattr(b, "text"))
