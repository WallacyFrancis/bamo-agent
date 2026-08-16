"""Memória de trabalho: sessões de conversa (seção 4.1 do PRD-002).

Uma sessão é um JSON por item em `sessions/<id>.json`. Retida por padrão
30 dias (D-005); a limpeza roda de forma oportunista em `cleanup_expired`,
chamada no início de `chat`, `ask` e `session list`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import ids
from .atomic import write_json_atomic
from .paths import SESSIONS_DIR
from .redact import redact

SCHEMA_VERSION = 1
RETENTION_DAYS = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def path_for(session_id: str) -> Path:
    ids.validate_id(session_id)
    return SESSIONS_DIR / f"{session_id}.json"


def create(cwd: Path) -> dict[str, Any]:
    session_id = ids.new_id("sess")
    now = _now()
    session: dict[str, Any] = {
        "id": session_id,
        "schema_version": SCHEMA_VERSION,
        "runtime": "agy",
        "cwd": str(cwd),
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "ended_at": None,
        "summary": "",
        "turns": [],
        "last_summarized_turn": 0,
        "last_extracted_turn": 0,
        "last_reflected_turn": 0,
    }
    save(session)
    return session


def save(session: dict[str, Any]) -> None:
    """Único ponto de escrita da sessão em disco — redige segredos aqui como
    última linha de defesa, mesmo que algum chamador já tenha redigido antes
    (redigir texto já redigido é inofensivo: não sobra padrão para casar)."""
    for turn in session.get("turns", []):
        turn["content"], _ = redact(turn.get("content", ""))
    if session.get("summary"):
        session["summary"], _ = redact(session["summary"])
    session["updated_at"] = _now()
    write_json_atomic(path_for(session["id"]), session)


def load(session_id: str) -> dict[str, Any] | None:
    path = path_for(session_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def exists(session_id: str) -> bool:
    try:
        return path_for(session_id).exists()
    except ids.InvalidIdError:
        return False


def append_turn(
    session: dict[str, Any],
    role: str,
    content: str,
    *,
    error: bool = False,
    retrieved_memory_ids: list[str] | None = None,
    retrieved_okf_ids: list[str] | None = None,
) -> int:
    """Adiciona um turno. `retrieved_memory_ids`/`retrieved_okf_ids` registram
    só o que efetivamente entrou no contexto enviado ao agy naquele turno
    (seção 4.1 do PRD-002) — itens cortados pelo orçamento de contexto não
    entram aqui. Sessões antigas sem esses campos continuam válidas: eles só
    aparecem quando há algo a registrar.
    """
    turn_number = len(session["turns"]) // 2 + 1
    turn: dict[str, Any] = {
        "turn": turn_number,
        "role": role,
        "content": content,
        "at": _now(),
        "error": error,
    }
    if retrieved_memory_ids:
        turn["retrieved_memory_ids"] = list(retrieved_memory_ids)
    if retrieved_okf_ids:
        turn["retrieved_okf_ids"] = list(retrieved_okf_ids)
    session["turns"].append(turn)
    save(session)
    return turn_number


def completed_turns(session: dict[str, Any]) -> int:
    """Número de trocas usuário+bamo completas (não conta turnos com erro)."""
    return sum(1 for t in session["turns"] if t["role"] == "bamo" and not t.get("error"))


def end(session: dict[str, Any]) -> None:
    session["status"] = "ended"
    session["ended_at"] = _now()
    save(session)


def delete(session_id: str) -> bool:
    path = path_for(session_id)
    if not path.exists():
        return False
    path.unlink()
    return True


@dataclass
class SessionSummary:
    id: str
    status: str
    created_at: str
    updated_at: str
    turn_count: int


def list_sessions() -> list[SessionSummary]:
    if not SESSIONS_DIR.exists():
        return []
    out: list[SessionSummary] = []
    for path in sorted(SESSIONS_DIR.glob("sess-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append(
            SessionSummary(
                id=data["id"],
                status=data["status"],
                created_at=data["created_at"],
                updated_at=data["updated_at"],
                turn_count=len(data["turns"]),
            )
        )
    return sorted(out, key=lambda s: s.created_at, reverse=True)


def cleanup_expired(retention_days: int = RETENTION_DAYS) -> int:
    if not SESSIONS_DIR.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for path in SESSIONS_DIR.glob("sess-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            reference = data.get("ended_at") or data["updated_at"]
            when = datetime.fromisoformat(reference)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if when < cutoff:
            path.unlink()
            removed += 1
    return removed
