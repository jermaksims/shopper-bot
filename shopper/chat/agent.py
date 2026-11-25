"""Terminal chat with Claude over the local deals database (Messages API + client-side tools).

The tools run locally against SQLite (read-only), so only small query results — never the whole
table — are sent to the model.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date

import anthropic
from rich.console import Console
from rich.markdown import Markdown

from shopper.chat.tools import TOOL_DEFS, call_tool

MODEL = os.environ.get("SHOPPER_MODEL", "claude-opus-5-5")

SYSTEM = f"""You are a shopping assistant for Black Friday clothing deals. Today is {date.today():%Y-%m-%d}.
You can only see products stored in a local database that a scraper fills from stores such as
Uniqlo, Ralph Lauren, Tommy Hilfiger and Macy's. Use the tools to look things up — never invent
products, prices or links.

How to answer:
- Reply in the user's language. Product titles in the database are in English, so translate the
  user's wording into English keywords for the `query` argument (e.g. "свитер" -> "sweater").
- For each recommended item give: title, store, current price, original price, discount %, and the URL.
- Rank by what the user asked for (cheapest, biggest discount, best value). If they gave a budget
  or size, respect it.
- If `suspicious` = 1 the claimed original price was never observed — say the discount may be
  inflated. If `lowest_seen` = 1, mention it's the lowest price tracked.
- If the search finds nothing, loosen the filters once (fewer keywords, no size) before saying so,
  and mention the data only covers stores that have been scraped (see db_overview).
- Be concise: a short ranked list beats a long essay."""


def run_chat(conn: sqlite3.Connection, console: Console | None = None) -> None:
    console = console or Console()
    client = anthropic.Anthropic()
    messages: list = []
    console.print("[bold]Shopper chat[/bold] — ask about deals (Ctrl+C or 'exit' to quit).")
    while True:
        try:
            user = console.input("[bold cyan]you> [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not user:
            continue
        if user.lower() in {"exit", "quit", "выход"}:
            return
        messages.append({"role": "user", "content": user})
        reply = turn(client, conn, messages, console)
        console.print(Markdown(reply or "_(no answer)_"))


def turn(client: anthropic.Anthropic, conn: sqlite3.Connection, messages: list, console: Console,
         max_steps: int = 12) -> str:
    """Run one user turn: call the model, execute requested tools, repeat until it answers."""
    for _ in range(max_steps):
        try:
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                tools=TOOL_DEFS,
                messages=messages,
                output_config={"effort": "medium"},
                cache_control={"type": "ephemeral"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError:
            return "**Error:** set `ANTHROPIC_API_KEY` (or run `ant auth login`)."
        except anthropic.RateLimitError:
            return "**Error:** rate limited by the API, try again in a minute."
        except anthropic.APIStatusError as e:
            return f"**API error {e.status_code}:** {e.message}"
        except anthropic.APIConnectionError:
            return "**Error:** could not reach the Anthropic API."

        # Keep the full content (incl. thinking blocks) so the conversation stays valid.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            return "The model declined this request."
        if response.stop_reason != "tool_use":
            text = "".join(b.text for b in response.content if b.type == "text")
            if response.stop_reason == "max_tokens":
                text += "\n\n_(answer truncated)_"
            return text

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            args = block.input if isinstance(block.input, dict) else json.loads(block.input or "{}")
            console.print(f"[dim]→ {block.name}({json.dumps(args, ensure_ascii=False)})[/dim]")
            try:
                out = call_tool(conn, block.name, args)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
            except Exception as e:  # bad arguments etc. — let the model correct itself
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"error: {e}", "is_error": True})
        # All results of one assistant turn go back in a single user message.
        messages.append({"role": "user", "content": results})
    return "_(stopped after too many tool calls)_"
