"""Memória de longo prazo: fatos duráveis extraídos automaticamente (seção 4.2/4.3).

Um JSON por memória em `memory/facts/<id>.json`. Conflitos nunca reescrevem
em lugar: a memória antiga vira `superseded` e aponta para a nova.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ids
from .atomic import write_json_atomic
from .paths import MEMORY_FACTS_DIR
from .redact import redact

VALID_TYPES = {"identidade", "preferencia", "projeto", "decisao", "padrao", "conhecimento"}
VALID_STATES = {"active", "superseded", "forgotten", "blocked"}
MAX_CONTENT_CHARS = 2000


class InvalidMemoryError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def path_for(memory_id: str) -> Path:
    ids.validate_id(memory_id)
    return MEMORY_FACTS_DIR / f"{memory_id}.json"


def validate_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Valida um candidato bruto (vindo do agy ou de correção manual) contra o
    schema local. Levanta InvalidMemoryError com o motivo em caso de falha.
    """
    tipo = candidate.get("tipo")
    conteudo = candidate.get("conteudo")
    tags = candidate.get("tags", [])
    importancia = candidate.get("importancia")
    confianca = candidate.get("confianca")

    if tipo not in VALID_TYPES:
        raise InvalidMemoryError(f"tipo inválido: {tipo!r}")
    if not isinstance(conteudo, str) or not conteudo.strip():
        raise InvalidMemoryError("conteudo vazio")
    if len(conteudo) > MAX_CONTENT_CHARS:
        raise InvalidMemoryError("conteudo excede o tamanho máximo")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise InvalidMemoryError("tags deve ser uma lista de strings")
    if not isinstance(importancia, int) or not (1 <= importancia <= 5):
        raise InvalidMemoryError(f"importancia inválida: {importancia!r}")
    if not isinstance(confianca, (int, float)) or not (0.0 <= float(confianca) <= 1.0):
        raise InvalidMemoryError(f"confianca inválida: {confianca!r}")

    return {
        "tipo": tipo,
        "conteudo": conteudo.strip(),
        "tags": [t.strip().lower() for t in tags if t.strip()],
        "importancia": int(importancia),
        "confianca": float(confianca),
        "justificativa": str(candidate.get("justificativa", ""))[:500],
    }


def create(
    validated: dict[str, Any],
    *,
    origem: dict[str, Any],
    supersedes: str | None = None,
) -> dict[str, Any]:
    memory_id = ids.new_id("mem")
    now = _now()
    memory: dict[str, Any] = {
        "id": memory_id,
        "tipo": validated["tipo"],
        "conteudo": validated["conteudo"],
        "tags": validated["tags"],
        "importancia": validated["importancia"],
        "confianca": validated["confianca"],
        "estado": "active",
        "criado_em": now,
        "confirmado_em": now,
        "origem": origem,
        "validade_inicial": now,
        "validade_final": None,
        "justificativa": validated["justificativa"],
        "supersedes": supersedes,
        "superseded_by": None,
        "reinforced_count": 0,
    }
    save(memory)
    if supersedes:
        old = load(supersedes)
        if old:
            old["estado"] = "superseded"
            old["superseded_by"] = memory_id
            old["validade_final"] = now
            save(old)
    return memory


def save(memory: dict[str, Any]) -> None:
    """Único ponto de escrita da memória em disco — redige segredos aqui como
    última linha de defesa, além de qualquer redação já feita pelo chamador."""
    memory["conteudo"], _ = redact(memory.get("conteudo", ""))
    memory["justificativa"], _ = redact(memory.get("justificativa", ""))
    memory["tags"] = [redact(tag)[0] for tag in memory.get("tags", [])]
    write_json_atomic(path_for(memory["id"]), memory)


def load(memory_id: str) -> dict[str, Any] | None:
    path = path_for(memory_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def reinforce(memory: dict[str, Any]) -> dict[str, Any]:
    memory["reinforced_count"] = memory.get("reinforced_count", 0) + 1
    memory["confirmado_em"] = _now()
    save(memory)
    return memory


def set_state(memory_id: str, state: str) -> dict[str, Any] | None:
    if state not in VALID_STATES:
        raise InvalidMemoryError(f"estado inválido: {state!r}")
    memory = load(memory_id)
    if not memory:
        return None
    memory["estado"] = state
    save(memory)
    return memory


def forget(memory_id: str) -> bool:
    path = path_for(memory_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def list_all(status: str | None = None) -> list[dict[str, Any]]:
    if not MEMORY_FACTS_DIR.exists():
        return []
    out = []
    for path in sorted(MEMORY_FACTS_DIR.glob("mem-*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if status is None or data["estado"] == status:
            out.append(data)
    return sorted(out, key=lambda m: m["criado_em"], reverse=True)


def list_active() -> list[dict[str, Any]]:
    return list_all(status="active")
