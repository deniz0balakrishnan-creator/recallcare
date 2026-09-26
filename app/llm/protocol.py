"""JSON tool-calling protocol (the gateway's native tool calling is unreliable — see the technical document, §3.1).

Every agent replies with ONE JSON object:
    {"thought_summary": str, "action": "<tool_name>|respond|handoff|escalate", "args": {...}}
We extract it robustly, validate `args` with the Pydantic model registered for that action, send ONE repair
prompt on failure, and give up (caller escalates "unparseable model output") on the second failure.
Nothing is executed unless it validated.
"""
from __future__ import annotations

import json
import re
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, Field, ValidationError

from app.llm.base import LLMClient, LLMResult


class AgentAction(BaseModel):
    thought_summary: str = Field(default="", max_length=300)
    action: str = Field(min_length=1, max_length=40)
    args: dict[str, Any] = Field(default_factory=dict)


class ProtocolError(Exception):
    pass


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)
_INVOKE = re.compile(r'<invoke\s+name="([^"]+)"\s*>(.*?)</invoke>', re.S)
_PARAM = re.compile(r'<parameter\s+name="([^"]+)"\s*>(.*?)</parameter>', re.S)


def _first_json_object(text: str) -> str | None:
    """Return the first balanced {...} block, respecting strings/escapes."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ProtocolError("empty response")
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for cand in candidates:
        blob = _first_json_object(cand)
        if blob:
            try:
                obj = json.loads(blob)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    inv = _INVOKE.search(text)            # Claude-Code-style XML the gateway sometimes emits
    if inv:
        args: dict[str, Any] = {}
        for k, v in _PARAM.findall(inv.group(2)):
            v = v.strip()
            try:
                args[k] = json.loads(v)
            except json.JSONDecodeError:
                args[k] = v
        return {"thought_summary": "", "action": inv.group(1), "args": args}
    raise ProtocolError("no JSON object found in the response")


def parse_action(text: str, allowed: dict[str, type[BaseModel] | None]) -> tuple[AgentAction, BaseModel | None]:
    obj = extract_json(text)
    if "action" not in obj and "tool" in obj:            # tolerate the starter kit's {"tool":…, "args":…} form
        obj["action"] = obj.pop("tool")
    try:
        act = AgentAction.model_validate(obj)
    except ValidationError as e:
        raise ProtocolError(_short_errors(e)) from e
    if act.action not in allowed:
        raise ProtocolError(f"action '{act.action}' is not allowed; choose one of: {', '.join(allowed)}")
    model = allowed[act.action]
    if model is None:
        return act, None
    try:
        return act, model.model_validate(act.args)
    except ValidationError as e:
        raise ProtocolError(f"invalid args for {act.action}: {_short_errors(e)}") from e


def _short_errors(e: ValidationError) -> str:
    parts = []
    for err in e.errors()[:4]:
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


# ---------------------------------------------------------------- compact tool signatures for prompts
def _type_str(ann: Any) -> str:
    origin = get_origin(ann)
    if origin is Literal:
        return "|".join(json.dumps(a, ensure_ascii=False) for a in get_args(ann))
    if origin in (typing.Union, types.UnionType):
        return " or ".join(_type_str(a) for a in get_args(ann) if a is not type(None)) + " (optional)"
    if origin in (list, tuple):
        inner = get_args(ann)
        return f"list[{_type_str(inner[0]) if inner else 'any'}]"
    if origin is dict:
        k, v = get_args(ann) or (str, Any)
        return f"dict[{_type_str(k)},{_type_str(v)}]"
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return ann.__name__
    return getattr(ann, "__name__", str(ann)).replace("typing.", "")


def signature(name: str, model: type[BaseModel] | None, desc: str | None = None) -> str:
    doc = desc or ((model.__doc__ or "").strip().splitlines()[0] if model and model.__doc__ else "")
    if model is None:
        return f"- {name}: {doc}"
    fields = []
    for fname, f in model.model_fields.items():
        s = f"{fname}: {_type_str(f.annotation)}"
        for m in f.metadata:
            for attr in ("max_length", "ge", "le"):
                val = getattr(m, attr, None)
                if val is not None:
                    s += f" {attr}={val}"
        fields.append(s)
    return f"- {name}({', '.join(fields)}): {doc}"


def tool_block(allowed: dict[str, type[BaseModel] | None], descriptions: dict[str, str] | None = None) -> str:
    descriptions = descriptions or {}
    lines = [signature(n, m, descriptions.get(n)) for n, m in allowed.items()]
    return "\n".join(lines)


REPAIR_PROMPT = (
    "Your previous reply could not be used: {error}\n"
    "Reply again with ONLY one JSON object, no prose, exactly this shape:\n"
    '{{"thought_summary": "<one short line>", "action": "<one of: {actions}>", "args": {{...}}}}'
)


@dataclass
class ProtocolOutcome:
    action: AgentAction | None
    args: BaseModel | None
    calls: list[LLMResult] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.action is not None


def call_with_repair(llm: LLMClient, system: str, messages: list[dict[str, str]],
                     allowed: dict[str, type[BaseModel] | None], *, agent: str,
                     context: dict[str, Any] | None = None, max_tokens: int | None = None) -> ProtocolOutcome:
    """One call, at most one repair call. Never raises for bad model output — returns error instead."""
    out = ProtocolOutcome(None, None)
    res = llm.complete(system, messages, agent=agent, context=context, max_tokens=max_tokens)
    out.calls.append(res)
    try:
        out.action, out.args = parse_action(res.text, allowed)
        return out
    except ProtocolError as e:
        first_err = str(e)
    repair_msgs = messages + [
        {"role": "assistant", "content": (res.text or "")[:600]},
        {"role": "user", "content": REPAIR_PROMPT.format(error=first_err[:300], actions=", ".join(allowed))},
    ]
    ctx = dict(context or {}, repair=True)
    res2 = llm.complete(system, repair_msgs, agent=agent, context=ctx, max_tokens=max_tokens)
    out.calls.append(res2)
    try:
        out.action, out.args = parse_action(res2.text, allowed)
    except ProtocolError as e:
        out.error = f"unparseable model output: {e}"
    return out
