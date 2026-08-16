"""Monta o prompt final enviado ao agy a cada turno (seção 4.4 / D-008).

Ordem: regras invioláveis + CORE.md, resumo + turnos recentes da sessão,
memórias ativas rankeadas para o pedido, e o OKF mais relevante (se houver).
Respeita os tetos de D-008: até 12 turnos recentes, até 12 memórias, até
12.000 caracteres de contexto antes do pedido atual. Se estourar mesmo após
cortar memórias, corta turnos mais antigos por último — o resumo nunca é
cortado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import knowledge_store, memory_store, ranking
from .paths import MEMORY_CORE, ROOT, SKILLS_DIR

MAX_RECENT_TURNS = 12
MAX_RETRIEVED_MEMORIES = 12
MAX_CONTEXT_CHARS = 12_000
OKF_RELEVANCE_THRESHOLD = 0.15


@dataclass
class BuiltContext:
    text: str
    memory_ids: list[str] = field(default_factory=list)
    okf_id: str | None = None


def _rules_header(skill_names: list[str]) -> str:
    memory = MEMORY_CORE.read_text(encoding="utf-8") if MEMORY_CORE.exists() else "(sem memória inicial)"
    return f"""Você é Bamo, um assistente local executado pelo runtime agy.

Seu workspace é: {ROOT}

Memória-base:
{memory}

Skills instaladas: {', '.join(skill_names) or '(nenhuma)'}.

Regras invioláveis:
1. Leia e siga as skills relevantes em {SKILLS_DIR}.
2. Nunca crie, edite, mova ou remova nada dentro de {SKILLS_DIR} sem pedido explícito do usuário.
3. Nunca altere, mova ou remova seu próprio código-fonte (bamo.py, core/) nem qualquer configuração do runtime agy (flags de invocação, arquivos de settings, sandbox/modo padrão) sem pedido explícito do usuário nesta conversa.
4. Você nunca precisa, e não deve tentar, executar comandos de terminal ou escrever/apagar arquivos por conta própria — apenas responda em texto. Toda a persistência (sessão, memória, conhecimento) é feita pelo código do Bamo, não por você.
5. Memórias de longo prazo e OKFs relevantes já aparecem abaixo, quando existirem; não invente memórias que não estejam listadas.
6. Nunca registre nem repita senhas, tokens, chaves de API ou outros segredos na resposta.
7. Seja transparente sobre ações, limites e incertezas.
"""


def _format_turns(turns: list[dict[str, Any]]) -> str:
    lines = []
    for turn in turns:
        if turn.get("error"):
            continue
        speaker = "Você" if turn["role"] == "user" else "Bamo"
        lines.append(f"{speaker}: {turn['content']}")
    return "\n".join(lines)


def _format_memories(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return "(nenhuma memória relevante recuperada para este pedido)"
    lines = []
    for m in memories:
        lines.append(
            f"- [{m['id']}] ({m['tipo']}, confiança {m['confianca']:.2f}, importância {m['importancia']}) {m['conteudo']}"
        )
    return "\n".join(lines)


def _best_okf(request: str) -> dict[str, Any] | None:
    okfs = [o for o in knowledge_store.list_all() if o["status"] == "active"]
    if not okfs:
        return None
    query_tokens = ranking.tokenize(request)
    best = None
    best_score = 0.0
    for okf in okfs:
        okf_tokens = ranking.tokenize(okf["title"]) | ranking.tokenize(okf["scope"])
        overlap = len(query_tokens & okf_tokens) / max(1, len(query_tokens))
        if overlap > best_score:
            best_score = overlap
            best = okf
    if best and best_score >= OKF_RELEVANCE_THRESHOLD:
        return best
    return None


def build(session: dict[str, Any], user_request: str) -> BuiltContext:
    """Monta o prompt e devolve, junto com o texto, os IDs de memórias e do OKF
    que sobreviveram ao corte de orçamento e realmente foram incluídos —
    é isso que o chamador deve gravar como referência de recuperação do turno
    (seção 4.1 do PRD-002). Itens descartados pelo orçamento não aparecem aqui.
    """
    skill_names = sorted(p.name for p in SKILLS_DIR.iterdir() if p.is_dir()) if SKILLS_DIR.exists() else []
    header = _rules_header(skill_names)

    recent_turns = session["turns"][-(MAX_RECENT_TURNS * 2) :]
    active_memories = memory_store.list_active()
    ranked_memories = ranking.rank(active_memories, user_request, limit=MAX_RETRIEVED_MEMORIES)
    okf = _best_okf(user_request)

    def render(turns: list[dict[str, Any]], memories: list[dict[str, Any]], include_okf: bool) -> str:
        parts = [header]
        if session.get("summary"):
            parts.append(f"Resumo da sessão até aqui:\n{session['summary']}")
        if turns:
            parts.append(f"Turnos recentes desta sessão:\n{_format_turns(turns)}")
        parts.append(f"Memórias de longo prazo relevantes para este pedido:\n{_format_memories(memories)}")
        if include_okf and okf:
            parts.append(
                f"OKF relacionado [{okf['id']}] {okf['title']} (confiança: {okf['confidence']}):\n{okf['body'][:1500]}"
            )
        parts.append(f"Pedido do usuário:\n{user_request}")
        return "\n\n".join(parts)

    include_okf = True
    text = render(recent_turns, ranked_memories, include_okf=include_okf)
    while len(text) > MAX_CONTEXT_CHARS and ranked_memories:
        ranked_memories = ranked_memories[:-1]
        text = render(recent_turns, ranked_memories, include_okf=include_okf)
    if len(text) > MAX_CONTEXT_CHARS:
        include_okf = False
        text = render(recent_turns, ranked_memories, include_okf=include_okf)
    while len(text) > MAX_CONTEXT_CHARS and len(recent_turns) > 2:
        recent_turns = recent_turns[2:]
        text = render(recent_turns, ranked_memories, include_okf=include_okf)

    okf_id = okf["id"] if (okf and include_okf) else None
    return BuiltContext(text=text, memory_ids=[m["id"] for m in ranked_memories], okf_id=okf_id)
