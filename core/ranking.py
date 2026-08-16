"""Pontuação determinística e local para recuperação de memórias (seção 4.4).

Sem embeddings nem banco vetorial nesta fase — combina sobreposição de
palavras/tags com o pedido, importância, recência, confiança e reforço.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

_WORD_RE = re.compile(r"[a-zà-ú0-9]+", re.IGNORECASE)

_WEIGHT_OVERLAP = 3.0
_WEIGHT_IMPORTANCE = 1.0
_WEIGHT_RECENCY = 1.5
_WEIGHT_CONFIDENCE = 1.0
_WEIGHT_REINFORCEMENT = 0.3
_RECENCY_HALF_LIFE_DAYS = 30.0


def tokenize(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) > 2}


def score(memory: dict[str, Any], query_tokens: set[str]) -> float:
    memory_tokens = set(memory.get("tags", [])) | tokenize(memory.get("conteudo", ""))
    overlap = len(query_tokens & memory_tokens)
    overlap_score = overlap / max(1, len(query_tokens)) if query_tokens else 0.0

    importance_score = memory.get("importancia", 1) / 5.0
    confidence_score = float(memory.get("confianca", 0.5))

    try:
        confirmed = datetime.fromisoformat(memory["confirmado_em"])
        age_days = max(0.0, (datetime.now(timezone.utc) - confirmed).total_seconds() / 86400)
    except (KeyError, ValueError):
        age_days = _RECENCY_HALF_LIFE_DAYS
    recency_score = math.exp(-age_days / _RECENCY_HALF_LIFE_DAYS)

    reinforcement_score = min(1.0, memory.get("reinforced_count", 0) / 5.0)

    return (
        _WEIGHT_OVERLAP * overlap_score
        + _WEIGHT_IMPORTANCE * importance_score
        + _WEIGHT_RECENCY * recency_score
        + _WEIGHT_CONFIDENCE * confidence_score
        + _WEIGHT_REINFORCEMENT * reinforcement_score
    )


def rank(memories: list[dict[str, Any]], request: str, *, limit: int) -> list[dict[str, Any]]:
    query_tokens = tokenize(request)
    scored = sorted(memories, key=lambda m: score(m, query_tokens), reverse=True)
    return scored[:limit]
