# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Write each Codex agent's context use and estimated cost to pane tokens for the Herdr sidebar.

`$codex_context` shows `ctx 7%` and `$codex_cost` shows `$0.533`, in the same form as the
Claude and pi tokens. Codex writes token counts to its session log; the cost uses pi's
price list, so Codex and pi price a model the same way.

With no event it covers every Codex pane (startup and the refresh action).
Driven by a Herdr event it covers only the pane that the event names.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import time


SOURCE = "nulluminati.codex-usage"
CONTEXT_TOKEN = "codex_context"
COST_TOKEN = "codex_cost"
# pi's providers for OpenAI models, in the order to look up a price.
PRICE_PROVIDERS = ("openai-codex", "openai")


def command(argv: list[str]):
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=30)


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or "herdr"


def herdr_json(*arguments: str) -> dict:
    return json.loads(command([herdr_bin(), *arguments]).stdout or "{}")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def pi_agent_dir() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")


def find_pane_id(value) -> str | None:
    # The pane id sits at a depth that varies by event, so search for it.
    if isinstance(value, dict):
        if isinstance(value.get("pane_id"), str):
            return value["pane_id"]
        value = list(value.values())
    if isinstance(value, list):
        for entry in value:
            found = find_pane_id(entry)
            if found:
                return found
    return None


def codex_agents() -> list[dict]:
    event = os.environ.get("HERDR_PLUGIN_EVENT_JSON")
    wanted = None
    if event:
        try:
            wanted = find_pane_id(json.loads(event))
        except json.JSONDecodeError:
            wanted = None
        if not wanted:
            return []
    agents = herdr_json("agent", "list").get("result", {}).get("agents") or []
    return [agent for agent in agents
            if agent.get("agent") == "codex" and isinstance(agent.get("pane_id"), str)
            and wanted in (None, agent["pane_id"])]


def session_log(agent: dict) -> Path | None:
    session = agent.get("agent_session") or {}
    value = session.get("value")
    if not isinstance(value, str) or not value:
        return None
    if session.get("kind") == "path":
        return Path(value) if Path(value).is_file() else None
    # Codex names a session log `rollout-<time>-<session id>.jsonl`, in a folder per day.
    found = sorted((codex_home() / "sessions").glob(f"*/*/*/rollout-*-{value}.jsonl"))
    return found[-1] if found else None


def read_usage(path: Path) -> tuple[str | None, list[dict], dict | None]:
    """The model, the token use of each model call, and the last token count with a context size."""
    model = None
    calls = []
    last = None
    seen_total = None
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = entry.get("payload") if isinstance(entry, dict) else None
            if not isinstance(payload, dict):
                continue
            if entry.get("type") == "turn_context" and isinstance(payload.get("model"), str):
                model = payload["model"]
            info = payload.get("info") if payload.get("type") == "token_count" else None
            if not isinstance(info, dict) or not isinstance(info.get("last_token_usage"), dict):
                continue
            # Codex can repeat a count without a new model call. The running total then stays the same.
            if info.get("total_token_usage") == seen_total:
                continue
            seen_total = info.get("total_token_usage")
            calls.append(info["last_token_usage"])
            last = info
    return model, calls, last


def load_price(model: str | None) -> dict | None:
    if not model:
        return None
    try:
        store = json.loads((pi_agent_dir() / "models-store.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for provider in PRICE_PROVIDERS:
        for entry in (store.get(provider) or {}).get("models") or []:
            if isinstance(entry, dict) and entry.get("id") == model and isinstance(entry.get("cost"), dict):
                return entry["cost"]
    return None


def call_cost(usage: dict, price: dict) -> float:
    """The cost of one model call in dollars, as pi's calculateCost works it out."""
    def count(name):
        value = usage.get(name)
        return value if isinstance(value, (int, float)) else 0

    cached = count("cached_input_tokens")
    written = count("cache_write_input_tokens")
    # Codex counts cached and cache-write tokens inside input_tokens; pi counts them apart.
    total_input = count("input_tokens")
    rates = price
    threshold = -1
    for tier in price.get("tiers") or []:
        above = tier.get("inputTokensAbove", -1)
        if total_input > above > threshold:
            rates, threshold = tier, above
    return ((total_input - cached - written) * rates.get("input", 0)
            + cached * rates.get("cacheRead", 0)
            + written * rates.get("cacheWrite", 0)
            + count("output_tokens") * rates.get("output", 0)) / 1_000_000


def tokens(agent: dict) -> tuple[str | None, str | None]:
    path = session_log(agent)
    if path is None:
        return None, None
    model, calls, last = read_usage(path)
    context = None
    window = (last or {}).get("model_context_window")
    used = ((last or {}).get("last_token_usage") or {}).get("total_tokens")
    if isinstance(window, (int, float)) and window > 0 and isinstance(used, (int, float)):
        context = f"ctx {round(100 * used / window)}%"
    price = load_price(model)
    cost = f"${sum(call_cost(call, price) for call in calls):.3f}" if price and calls else None
    return context, cost


def report(pane_id: str, context: str | None, cost: str | None) -> None:
    changes = []
    for name, value in ((CONTEXT_TOKEN, context), (COST_TOKEN, cost)):
        changes += ["--token", f"{name}={value}"] if value else ["--clear-token", name]
    command([herdr_bin(), "pane", "report-metadata", pane_id,
             "--source", SOURCE, "--seq", str(time.time_ns()), *changes])


def failure(error: Exception) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        return (error.stderr or "").strip() or str(error)
    return str(error)


def main() -> None:
    # A startup or event hook has no one to show an error to, so log it and go on.
    try:
        agents = codex_agents()
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print(f"codex-usage: cannot list agents: {failure(error)}", file=sys.stderr)
        return
    for agent in agents:
        pane_id = agent["pane_id"]
        try:
            report(pane_id, *tokens(agent))
        except (OSError, subprocess.SubprocessError) as error:
            print(f"codex-usage: {pane_id}: {failure(error)}", file=sys.stderr)


if __name__ == "__main__":
    main()
