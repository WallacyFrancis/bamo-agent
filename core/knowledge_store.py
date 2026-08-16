"""OKF — Organized Knowledge File (seção 4.5 do PRD-002, definido em knowledge/README.md).

Cada OKF é um Markdown em `knowledge/okf/<id>.md`: um front-matter simples
(uma linha `chave: <valor-json>` por campo, sem depender de biblioteca YAML)
seguido do corpo em Markdown.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ids
from .atomic import write_text_atomic
from .paths import KNOWLEDGE_OKF_DIR
from .redact import redact

VALID_CONFIDENCE = {"verified", "inference", "hypothesis"}
VALID_STATUS = {"active", "archived", "forgotten"}

_FRONTMATTER_FIELDS = [
    "id",
    "title",
    "created_at",
    "consulted_at",
    "sources",
    "scope",
    "confidence",
    "session_refs",
    "status",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def path_for(okf_id: str) -> Path:
    ids.validate_id(okf_id)
    return KNOWLEDGE_OKF_DIR / f"{okf_id}.md"


def _render(meta: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for field in _FRONTMATTER_FIELDS:
        lines.append(f"{field}: {json.dumps(meta[field], ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    lines.append(body.strip())
    lines.append("")
    return "\n".join(lines)


def _parse(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("OKF sem front-matter")
    meta: dict[str, Any] = {}
    idx = 1
    while idx < len(lines) and lines[idx].strip() != "---":
        line = lines[idx]
        if ": " in line:
            key, _, raw_value = line.partition(": ")
            try:
                meta[key.strip()] = json.loads(raw_value)
            except json.JSONDecodeError:
                meta[key.strip()] = raw_value.strip()
        idx += 1
    body = "\n".join(lines[idx + 1 :]).strip()
    return meta, body


def create(
    *,
    title: str,
    sources: list[dict[str, str]],
    scope: str,
    confidence: str,
    body: str,
    session_refs: list[str],
) -> dict[str, Any]:
    if confidence not in VALID_CONFIDENCE:
        raise ValueError(f"confidence inválida: {confidence!r}")

    # Única porta de escrita do OKF: redige segredos em todo campo textual
    # (título, escopo, corpo e fontes) como última linha de defesa, além de
    # qualquer redação já feita pelo chamador.
    title, _ = redact(title.strip())
    scope, _ = redact(scope.strip())
    body, _ = redact(body)
    redacted_sources = []
    for source in sources:
        url, _ = redact(str(source.get("url", "")))
        source_title, _ = redact(str(source.get("title", "")))
        redacted_sources.append({"url": url, "title": source_title})

    okf_id = ids.new_id("okf")
    now = _now()
    meta = {
        "id": okf_id,
        "title": title,
        "created_at": now,
        "consulted_at": now,
        "sources": redacted_sources,
        "scope": scope,
        "confidence": confidence,
        "session_refs": session_refs,
        "status": "active",
    }
    write_text_atomic(path_for(okf_id), _render(meta, body))
    meta["body"] = body
    return meta


def load(okf_id: str) -> dict[str, Any] | None:
    path = path_for(okf_id)
    if not path.exists():
        return None
    meta, body = _parse(path.read_text(encoding="utf-8"))
    meta["body"] = body
    return meta


def forget(okf_id: str) -> bool:
    path = path_for(okf_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def list_all() -> list[dict[str, Any]]:
    if not KNOWLEDGE_OKF_DIR.exists():
        return []
    out = []
    for path in sorted(KNOWLEDGE_OKF_DIR.glob("okf-*.md")):
        meta, _ = _parse(path.read_text(encoding="utf-8"))
        out.append(meta)
    return sorted(out, key=lambda m: m["created_at"], reverse=True)
