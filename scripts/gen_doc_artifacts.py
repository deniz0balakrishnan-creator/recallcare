"""Generate doc fragments from the real code and real runs (so the technical document can't drift from the system).

    .venv/bin/python scripts/gen_doc_artifacts.py

Writes docs/generated/: tool_catalogue.md, eval_results.md, trace_excerpt.md, graph.mmd
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "generated"


def _pattern(p: str) -> str:
    """Regexes with a literal pipe can't sit in a Markdown pipe table; show those in words."""
    if "|" in p:
        return "format YYYY-MM-DDTHH:MM + dentist id, e.g. 2026-09-29T09:00 / D1 (regex-checked)"
    return f"pattern `{p}`"


def tool_catalogue() -> str:
    from app.llm.protocol import _type_str
    from app.tools.registry import AGENT_TOOLS, TOOLS
    owners = {name: [a for a, ts in AGENT_TOOLS.items() if name in ts] for name in TOOLS}
    lines = ["| Tool | Agent (only) | Access | Typed arguments (Pydantic) | Purpose |",
             "|--------------|------|-----|--------------------------|------------|"]
    for name, spec in TOOLS.items():
        fields = []
        for fname, f in spec.schema.model_fields.items():
            s = f"`{fname}`: " + _type_str(f.annotation).replace("|", " / ").replace('"', "")
            cons = []
            for m in f.metadata:
                for attr in ("max_length", "min_length", "ge", "le", "pattern"):
                    v = getattr(m, attr, None)
                    if v is not None:
                        cons.append(_pattern(str(v)) if attr == "pattern" else f"{attr}={v}")
            if cons:
                s += f" ({', '.join(cons)})"
            fields.append(s)
        doc = (spec.schema.__doc__ or "").strip().split("\n")[0]
        lines.append(f"| `{name}` | {', '.join(owners[name])} | {spec.access} | {'; '.join(fields) or '*(none — scoped to current patient)*'} | {doc} |")
    return "\n".join(lines) + "\n"


def _report() -> dict | None:
    for m in ("live", "mock"):
        f = ROOT / "evals" / f"report_{m}.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    return None


def eval_results() -> str:
    rep = _report()
    if not rep:
        return "_No eval report yet._\n"
    lines = [f"Mode **{rep['mode']}**, model `{rep['model']}`, generated {rep['generated_at']}. "
             f"LLM calls {rep['llm_calls']}, tokens in/out {rep['tokens_in']:,}/{rep['tokens_out']:,}"
             f"{' (mock estimates)' if rep['tokens_estimated'] else ''}, estimated cost US${rep['est_cost_usd']}.", "",
             "| Category | Passed | Total | Pass rate |", "|---|---|---|---|"]
    for c, v in rep["by_category"].items():
        lines.append(f"| {c} | {v['passed']} | {v['total']} | {v['pass_rate']}% |")
    sc = rep["safety_critical"]
    lines.append(f"| **safety-critical** | {sc['passed']} | {sc['total']} | {sc['pass_rate']}% |")
    lines += ["", "| Scenario | Result | LLM calls | Tokens |", "|------------------------|--------|---|---|"]
    for r in rep["results"]:
        lines.append(f"| {r['id']} — {r['description']} | {'PASS' if r['passed'] else 'FAIL: ' + '; '.join(r['failures'])} | "
                     f"{r['llm_calls']} | {r['tokens_in'] + r['tokens_out']:,} |")
    return "\n".join(lines) + "\n"


def trace_excerpt(sid: str = "A06_hidden_symptom_in_booking") -> str:
    rep = _report()
    mode = rep["mode"] if rep else "mock"
    f = ROOT / "evals" / "runs" / mode / f"{sid}.json"
    if not f.exists():
        return "_Trace not available yet._\n"
    dump = json.loads(f.read_text(encoding="utf-8"))
    inbound_runs = [r["run_id"] for r in dump["runs"] if r["event_type"] == "inbound_message"]
    rows = [e for e in dump["trace"] if e["run_id"] in inbound_runs[-1:]]
    lines = [f"Real trace of eval scenario `{sid}` ({mode} mode) — patient wrote: "
             f"*\"{next((m['body'] for m in dump['messages'] if m['direction'] == 'in'), '')}\"*", "",
             "| Agent | Action | Outcome | Model / tokens | Rationale |", "|------|---------|-----|---------|----------------|"]
    for e in rows:
        v = e.get("guard_verdict")
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except json.JSONDecodeError:
                v = None
        why = e.get("thought_summary") or ""
        if v:
            why = f"verdict **{v['category']} → {v['action']}** (source {v['source']}, urgency {v['urgency']})"
        model = f"{e['model']} · {e['tokens_in']}/{e['tokens_out']}" if e.get("model") else ""
        lines.append(f"| {e['agent']} | `{e['action']}` | {e['outcome']} | {model} | {why.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tool_catalogue.md").write_text(tool_catalogue(), encoding="utf-8")
    (OUT / "eval_results.md").write_text(eval_results(), encoding="utf-8")
    (OUT / "trace_excerpt.md").write_text(trace_excerpt(), encoding="utf-8")
    (OUT / "trace_excerpt_booking.md").write_text(trace_excerpt("G01_en_books_tuesday_morning"), encoding="utf-8")
    from app.graph import mermaid
    (OUT / "graph.mmd").write_text(mermaid(), encoding="utf-8")
    print("wrote", ", ".join(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
