"""Extração, consolidação, reflexão e resumo — seção 4.3 do PRD-002.

Todas as chamadas ao agy aqui pedem texto puro contendo só um JSON (nunca
--output-format json/stream-json, que falha neste ambiente — ver
core/agy_runtime.py). Falhas de chamada ou de validação são silenciosas
para quem está conversando: o pior caso é não aprender nada naquele ciclo,
a conversa nunca é interrompida por isso (D-009).
"""

from __future__ import annotations

import difflib
import json
import sys
from typing import Any

from . import agy_runtime, knowledge_store, memory_store, sessions
from .atomic import write_json_atomic
from .paths import ROOT, SETTINGS_PATH
from .redact import redact

REFLECTION_EVERY_N_TURNS = 10
SUMMARY_EVERY_N_TURNS = 5
DEDUP_SIMILARITY_THRESHOLD = 0.82

_SENSITIVE_KEYWORDS = {
    "saúde", "saude", "doença", "doenca", "diagnóstico", "diagnostico", "medicamento",
    "biometria", "biométrico", "biometrico", "digital", "íris", "iris", "reconhecimento facial",
    "religião", "religiao", "religioso", "igreja", "fé", "crença religiosa",
    "político", "politico", "partido", "voto", "eleição", "eleicao",
    "sexual", "orientação sexual", "orientacao sexual",
    "conta bancária", "conta bancaria", "cartão de crédito", "cartao de credito", "cpf", "rg", "saldo bancário",
    "processo judicial", "advogado", "réu", "reu", "condenação", "condenacao",
}


def _log_warning(message: str) -> None:
    print(f"[bamo][aprendizado] {message}", file=sys.stderr)


def get_learning_enabled() -> bool:
    if not SETTINGS_PATH.exists():
        return True
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return True
    return bool(data.get("learning_enabled", True))


def set_learning_enabled(enabled: bool) -> None:
    # Lê-modifica-grava: settings.local.json também guarda `color_mode`/
    # `plain` (PRD-007, core/ui.py) desde que esse arquivo passou a ter
    # mais de uma chave — sobrescrever tudo aqui apagaria essas preferências.
    data: dict = {}
    if SETTINGS_PATH.exists():
        try:
            loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            pass
    data["learning_enabled"] = enabled
    write_json_atomic(SETTINGS_PATH, data)


def _looks_sensitive(text: str, tags: list[str]) -> bool:
    haystack = (text + " " + " ".join(tags)).lower()
    return any(keyword in haystack for keyword in _SENSITIVE_KEYWORDS)


def _extract_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    start_candidates = [i for i in (stripped.find("["), stripped.find("{")) if i != -1]
    if not start_candidates:
        raise ValueError("resposta sem JSON reconhecível")
    start = min(start_candidates)
    end_char = "]" if stripped[start] == "[" else "}"
    end = stripped.rfind(end_char)
    if end == -1 or end < start:
        raise ValueError("JSON malformado")
    return json.loads(stripped[start : end + 1])


def _new_turns_text(session: dict[str, Any], since_turn: int) -> str:
    lines = []
    for turn in session["turns"]:
        if turn["turn"] <= since_turn or turn.get("error"):
            continue
        speaker = "Você" if turn["role"] == "user" else "Bamo"
        lines.append(f"{speaker}: {turn['content']}")
    return "\n".join(lines)


def propose_candidates(session: dict[str, Any], since_turn: int) -> list[dict[str, Any]]:
    transcript = _new_turns_text(session, since_turn)
    if not transcript.strip():
        return []

    prompt = f"""Analise a conversa abaixo e extraia SOMENTE fatos duráveis sobre o
usuário, seus projetos ou preferências, que tenham evidência clara no texto.

Categorias permitidas: identidade, preferencia, projeto, decisao, padrao, conhecimento.
NÃO extraia: conversa casual, hipótese sem evidência, instrução de um único turno,
dado de terceiros sem relação com o usuário, ou qualquer inferência sobre saúde,
biometria, religião, política, sexualidade, finanças ou dados jurídicos.

Se o tipo for "conhecimento" e vier de uma pesquisa com fontes citadas na conversa,
inclua também "fontes": [{{"url": "...", "titulo": "..."}}]; sem fontes, não use o
tipo "conhecimento" para conteúdo não verificável.

Responda SOMENTE com um array JSON (pode ser vazio: []), sem markdown, sem texto
antes ou depois, no formato exato:
[{{"tipo": "preferencia", "conteudo": "...", "tags": ["..."], "importancia": 1,
"confianca": 0.8, "justificativa": "..."}}]

Conversa:
{transcript}
"""
    result = agy_runtime.call(prompt, cwd=ROOT)
    if not result.ok:
        _log_warning(f"extração falhou: {result.error}")
        return []
    try:
        candidates = _extract_json(result.text)
    except (ValueError, json.JSONDecodeError) as exc:
        _log_warning(f"extração retornou JSON inválido: {exc}")
        return []
    if not isinstance(candidates, list):
        return []
    return candidates


def _find_similar_active(validated: dict[str, Any]) -> tuple[dict[str, Any], float] | None:
    """Casa near-duplicates por texto (mesmo tipo, alta similaridade de conteúdo).
    Só cobre reformulações do mesmo fato; conflitos entre fatos com redações bem
    diferentes (ex.: "respostas curtas" vs "respostas detalhadas") são pegos por
    `reflect`, que olha sobreposição de tags entre memórias ativas de qualquer
    sessão, não só similaridade textual.
    """
    best_match = None
    best_ratio = 0.0
    for memory in memory_store.list_active():
        if memory["tipo"] != validated["tipo"]:
            continue
        ratio = difflib.SequenceMatcher(None, memory["conteudo"], validated["conteudo"]).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = memory
    if best_match and best_ratio >= 0.5:
        return best_match, best_ratio
    return None


def _related_active(memory: dict[str, Any]) -> list[dict[str, Any]]:
    """Outras memórias ativas do mesmo tipo com pelo menos uma tag em comum,
    de qualquer sessão — candidatas a conflito para a reflexão."""
    tags = set(memory.get("tags", []))
    return [
        m
        for m in memory_store.list_active()
        if m["id"] != memory["id"] and m["tipo"] == memory["tipo"] and tags & set(m.get("tags", []))
    ]


def _create_okf(raw: dict[str, Any], session: dict[str, Any]) -> dict[str, Any] | None:
    """Pesquisa durável com fontes vira OKF em vez de memória curta (seção 4.5)."""
    sources = []
    for f in raw.get("fontes", []):
        if isinstance(f, dict) and f.get("url"):
            sources.append({"url": str(f["url"])[:500], "title": str(f.get("titulo", ""))[:200]})
    if not sources:
        return None

    conteudo = str(raw.get("conteudo", "")).strip()
    if not conteudo:
        return None
    if _looks_sensitive(conteudo, raw.get("tags", []) or []):
        _log_warning("OKF descartado (categoria sensível)")
        return None

    confianca_raw = raw.get("confianca")
    confianca = float(confianca_raw) if isinstance(confianca_raw, (int, float)) else 0.5
    if confianca >= 0.8:
        confidence = "verified"
    elif confianca >= 0.5:
        confidence = "inference"
    else:
        confidence = "hypothesis"

    body, _ = redact(conteudo)
    return knowledge_store.create(
        title=conteudo[:120],
        sources=sources,
        scope=str(raw.get("justificativa", ""))[:300] or "Pesquisa registrada em conversa",
        confidence=confidence,
        body=body,
        session_refs=[session["id"]],
    )


def consolidate(session: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    origin = {"session_id": session["id"], "turn_ids": [t["turn"] for t in session["turns"] if not t.get("error")]}
    applied: list[dict[str, Any]] = []

    for raw in candidates:
        if not isinstance(raw, dict):
            continue

        if raw.get("tipo") == "conhecimento" and raw.get("fontes"):
            okf = _create_okf(raw, session)
            if okf:
                applied.append(okf)
                continue

        try:
            validated = memory_store.validate_candidate(raw)
        except memory_store.InvalidMemoryError as exc:
            _log_warning(f"candidato descartado ({exc})")
            continue

        if _looks_sensitive(validated["conteudo"], validated["tags"]):
            _log_warning("candidato descartado (categoria sensível)")
            continue

        redacted_content, was_redacted = redact(validated["conteudo"])
        validated["conteudo"] = redacted_content
        if was_redacted:
            validated["confianca"] = min(validated["confianca"], 0.5)

        match = _find_similar_active(validated)
        if match is None:
            memory = memory_store.create(validated, origem=origin)
            applied.append(memory)
        else:
            similar, ratio = match
            if ratio >= DEDUP_SIMILARITY_THRESHOLD:
                applied.append(memory_store.reinforce(similar))
            else:
                memory = memory_store.create(validated, origem=origin, supersedes=similar["id"])
                applied.append(memory)

    return applied


def reflect(session: dict[str, Any]) -> None:
    session_touched = [
        m
        for m in memory_store.list_active()
        if m.get("origem", {}).get("session_id") == session["id"]
    ]
    if not session_touched:
        return

    by_id = {m["id"]: m for m in session_touched}
    for m in session_touched:
        for related in _related_active(m):
            by_id[related["id"]] = related
    touched = list(by_id.values())

    listing = "\n".join(f"- [{m['id']}] {m['conteudo']} (importância {m['importancia']})" for m in touched)
    prompt = f"""Revise estas memórias tocadas na sessão atual e decida, para cada uma,
se ela deve ser elevada em importância (por ter sido reforçada/repetida), ou se
ela conflita com outra sendo descrita. Não crie metas nem tarefas.

Responda SOMENTE com um array JSON (pode ser vazio: []), sem markdown, no formato:
[{{"acao": "elevar", "id": "mem-...", "motivo": "..."}}]
ou
[{{"acao": "conflito", "id": "mem-...", "novo_conteudo": "...", "motivo": "..."}}]

Memórias:
{listing}
"""
    result = agy_runtime.call(prompt, cwd=ROOT)
    if not result.ok:
        _log_warning(f"reflexão falhou: {result.error}")
        return
    try:
        actions = _extract_json(result.text)
    except (ValueError, json.JSONDecodeError) as exc:
        _log_warning(f"reflexão retornou JSON inválido: {exc}")
        return
    if not isinstance(actions, list):
        return

    for action in actions:
        if not isinstance(action, dict):
            continue
        memory_id = action.get("id")
        memory = memory_store.load(memory_id) if isinstance(memory_id, str) else None
        if not memory or memory["estado"] != "active":
            continue
        if action.get("acao") == "elevar":
            memory["importancia"] = min(5, memory["importancia"] + 1)
            memory_store.reinforce(memory)
        elif action.get("acao") == "conflito" and isinstance(action.get("novo_conteudo"), str):
            try:
                validated = memory_store.validate_candidate(
                    {
                        "tipo": memory["tipo"],
                        "conteudo": action["novo_conteudo"],
                        "tags": memory["tags"],
                        "importancia": memory["importancia"],
                        "confianca": memory["confianca"],
                        "justificativa": action.get("motivo", "reflexão"),
                    }
                )
            except memory_store.InvalidMemoryError:
                continue
            redacted_content, _ = redact(validated["conteudo"])
            validated["conteudo"] = redacted_content
            origin = {"session_id": session["id"], "turn_ids": []}
            memory_store.create(validated, origem=origin, supersedes=memory["id"])


def update_summary(session: dict[str, Any]) -> None:
    transcript = _new_turns_text(session, session.get("last_summarized_turn", 0))
    if not transcript.strip():
        return
    prompt = f"""Resumo atual da sessão:
{session.get('summary') or '(nenhum ainda)'}

Novos turnos:
{transcript}

Atualize o resumo em até 5 frases, em texto puro (sem JSON, sem markdown),
priorizando fatos estáveis, decisões e o estado atual da conversa."""
    result = agy_runtime.call(prompt, cwd=ROOT)
    if not result.ok:
        _log_warning(f"resumo falhou: {result.error}")
        return
    session["summary"] = result.text.strip()


def run_cycle(session: dict[str, Any], *, final: bool) -> None:
    """Roda resumo/extração/reflexão conforme os gatilhos de turno, e sempre
    ao encerrar a sessão (final=True)."""
    completed = sum(1 for t in session["turns"] if t["role"] == "bamo" and not t.get("error"))

    due_summary = final or (
        completed > 0 and completed % SUMMARY_EVERY_N_TURNS == 0
    )
    if due_summary and completed > session.get("last_summarized_turn", 0):
        update_summary(session)
        session["last_summarized_turn"] = completed

    if not get_learning_enabled():
        sessions.save(session)
        return

    since_turn = session.get("last_extracted_turn", 0)
    if completed > since_turn:
        candidates = propose_candidates(session, since_turn)
        if candidates:
            consolidate(session, candidates)
        session["last_extracted_turn"] = completed

    due_reflection = final or (
        completed > 0 and completed % REFLECTION_EVERY_N_TURNS == 0
    )
    if due_reflection and completed > session.get("last_reflected_turn", 0):
        reflect(session)
        session["last_reflected_turn"] = completed

    sessions.save(session)
