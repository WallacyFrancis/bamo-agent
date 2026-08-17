"""Auditoria de execuções de conectores (PRD-004, seção 7).

Um arquivo JSONL por mês em `connector-audit/YYYY-MM.jsonl` (0600,
diretório 0700, ignorado pelo Git), no mesmo espírito de retenção das
sessões do PRD-002. Cada evento é redigido e não contém valor de cofre,
rótulo de segredo, payload bruto, headers, cookies, URL com query nem dado
pessoal retornado pelo serviço — só intenção, conector, capacidade, origem,
horários e um resumo redigido limitado a 2.000 caracteres.

Reescrita atômica do arquivo do mês inteiro a cada evento (via
`write_bytes_atomic_secure`) em vez de append em texto puro: garante que
uma escrita interrompida no meio nunca deixa uma linha JSONL truncada.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .atomic import ensure_dir_secure, write_bytes_atomic_secure
from .paths import CONNECTOR_AUDIT_DIR

RETENTION_DAYS = 30
MAX_SUMMARY_CHARS = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _month_path(dt: datetime) -> Path:
    return CONNECTOR_AUDIT_DIR / f"{dt:%Y-%m}.jsonl"


def append_event(
    *,
    connector_id: str,
    capability: str,
    origin: str,
    started_at: str,
    ended_at: str,
    ok: bool,
    status: str,
    summary: str = "",
) -> None:
    event = {
        "at": ended_at,
        "connector_id": connector_id,
        "capability": capability,
        "origin": origin,
        "started_at": started_at,
        "ended_at": ended_at,
        "ok": bool(ok),
        "status": status,
        "summary": (summary or "")[:MAX_SUMMARY_CHARS],
    }
    path = _month_path(_now())
    ensure_dir_secure(CONNECTOR_AUDIT_DIR)
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    lines.append(json.dumps(event, ensure_ascii=False))
    write_bytes_atomic_secure(path, ("\n".join(lines) + "\n").encode("utf-8"))


def list_events(connector_id: str | None = None) -> list[dict[str, Any]]:
    """Leitura best-effort por linha: um arquivo de um mês corrompido não
    derruba a listagem inteira — só pula esse mês (diferente do registro de
    conector/agenda, onde um único arquivo por entidade corrompido já é
    motivo de falha fechada; aqui são só eventos históricos)."""
    if not CONNECTOR_AUDIT_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(CONNECTOR_AUDIT_DIR.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if connector_id and event.get("connector_id") != connector_id:
                continue
            out.append(event)
    return sorted(out, key=lambda e: e.get("at", ""), reverse=True)


def cleanup_expired(retention_days: int = RETENTION_DAYS) -> int:
    if not CONNECTOR_AUDIT_DIR.exists():
        return 0
    cutoff = _now() - timedelta(days=retention_days)
    removed = 0
    for path in CONNECTOR_AUDIT_DIR.glob("*.jsonl"):
        try:
            month = datetime.strptime(path.stem, "%Y-%m").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        # O mês inteiro expira de uma vez, no fim do mês seguinte ao nome
        # do arquivo (ex.: "2026-06.jsonl" expira quando 2026-07-01 + 30
        # dias já passou) — evita apagar eventos do início do mês corrente.
        month_end = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        if month_end < cutoff:
            path.unlink()
            removed += 1
    return removed
