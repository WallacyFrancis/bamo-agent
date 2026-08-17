"""Resumo operacional e painel de saúde (PRD-006, seções 5 e 6).

`process_event()` é o "classificador operacional" do diagrama da seção 5:
único ponto que recebe um evento já gravado em `connector-audit/`
(PRD-004/005) e produz, de um só lugar, tanto o alerta (`core/alerts.py`)
quanto a linha correspondente em `operations/YYYY-MM.jsonl`. Este módulo
não abre o cofre, não importa executor algum, não chama `agy` e não tem
rede (seção 7.1) — só lê `connectors`/`scheduler`/`connector_audit`, que já
são, eles mesmos, livres de segredo.

`operations/YYYY-MM.jsonl` guarda só metadados estruturados — tempo,
conector, capacidade, agenda, origem, status e severidade — nunca o
`summary` redigido do evento de auditoria; quem quiser o resumo completo
usa `bamo connector audit`, que já existe desde o PRD-004 (seção 6:
"`operations list` ... não mostra resumo remoto por padrão").
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import alerts, connector_audit, connectors, scheduler
from .atomic import ensure_dir_secure, write_bytes_atomic_secure
from .paths import OPERATIONS_DIR

RETENTION_DAYS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _month_path(dt: datetime) -> Path:
    return OPERATIONS_DIR / f"{dt:%Y-%m}.jsonl"


def append_summary(
    *,
    connector_id: str,
    capability: str,
    schedule_id: str | None,
    origin: str,
    ok: bool,
    status: str,
    severity: str,
    at: str,
) -> None:
    row = {
        "at": at,
        "connector_id": connector_id,
        "capability": capability,
        "schedule_id": schedule_id,
        "origin": origin,
        "ok": bool(ok),
        "status": status,
        "severity": severity,
    }
    path = _month_path(_now())
    ensure_dir_secure(OPERATIONS_DIR)
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    lines.append(json.dumps(row, ensure_ascii=False))
    write_bytes_atomic_secure(path, ("\n".join(lines) + "\n").encode("utf-8"))


def process_event(
    *,
    connector_id: str,
    capability: str,
    schedule_id: str | None,
    origin: str,
    ok: bool,
    status: str,
    ended_at: str,
) -> tuple[dict[str, Any] | None, bool]:
    """Chamado uma vez por execução (manual ou por agenda), logo após o
    evento já ter sido gravado em `connector-audit/`. Grava a linha de
    `operations/` e delega a classificação/dedup a `alerts.record_event`.
    Retorna `(alerta_ou_None, deve_anunciar)`, repassado de
    `alerts.record_event` — usado por `bamo scheduler run` para decidir se
    imprime uma linha `ALERTA`."""
    alert, should_announce = alerts.record_event(
        connector_id=connector_id,
        capability=capability,
        schedule_id=schedule_id,
        origin=origin,
        ok=ok,
        status=status,
        ended_at=ended_at,
    )
    severity = "info" if ok else (alert["severity"] if alert else "warning")
    append_summary(
        connector_id=connector_id,
        capability=capability,
        schedule_id=schedule_id,
        origin=origin,
        ok=ok,
        status=status,
        severity=severity,
        at=ended_at,
    )
    return alert, should_announce


def list_events(connector_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Leitura best-effort por linha, igual a `connector_audit.list_events`
    — um mês corrompido não derruba a listagem (seção 7.4)."""
    if not OPERATIONS_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(OPERATIONS_DIR.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if connector_id and row.get("connector_id") != connector_id:
                continue
            out.append(row)
    out.sort(key=lambda r: r.get("at", ""), reverse=True)
    if limit is not None and limit >= 0:
        out = out[:limit]
    return out


def cleanup_expired(retention_days: int = RETENTION_DAYS) -> int:
    if not OPERATIONS_DIR.exists():
        return 0
    cutoff = _now() - timedelta(days=retention_days)
    removed = 0
    for path in OPERATIONS_DIR.glob("*.jsonl"):
        try:
            month = datetime.strptime(path.stem, "%Y-%m").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        month_end = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        if month_end < cutoff:
            path.unlink()
            removed += 1
    return removed


def status(connector_id: str | None = None) -> list[dict[str, Any]]:
    """Painel de saúde (`bamo status`, seção 6): só lê registros já
    validados de `connectors`/`scheduler`/`connector_audit`/`alerts` — não
    desbloqueia o cofre, não usa rede, não chama `agy` (critério de
    aceite, seção 8)."""
    out: list[dict[str, Any]] = []
    for connector in connectors.list_all():
        if connector_id and connector["id"] != connector_id:
            continue

        connector_schedules = [s for s in scheduler.list_all() if s["connector_id"] == connector["id"]]
        last_events = connector_audit.list_events(connector["id"])
        last_event = last_events[0] if last_events else None
        open_alerts = [
            a for a in alerts.list_all(connector_id=connector["id"]) if a["state"] in ("open", "acknowledged")
        ]

        schedules_view = []
        for sched in connector_schedules:
            next_due = None
            if sched["enabled"]:
                if sched["last_run_at"] is None:
                    next_due = "agora"
                else:
                    try:
                        last_run = datetime.fromisoformat(sched["last_run_at"])
                        next_due = (last_run + timedelta(minutes=sched["every_minutes"])).isoformat()
                    except ValueError:
                        next_due = None
            schedules_view.append(
                {
                    "id": sched["id"],
                    "capability": sched["capability"],
                    "every_minutes": sched["every_minutes"],
                    "enabled": sched["enabled"],
                    "last_run_at": sched["last_run_at"],
                    "last_status": sched["last_status"],
                    "next_due": next_due,
                }
            )

        out.append(
            {
                "connector_id": connector["id"],
                "display_name": connector["display_name"],
                "provider": connector["provider"],
                "enabled": connector["enabled"],
                "last_execution": (
                    {
                        "at": last_event["at"],
                        "capability": last_event["capability"],
                        "origin": last_event["origin"],
                        "ok": last_event["ok"],
                        "status": last_event["status"],
                    }
                    if last_event
                    else None
                ),
                "schedules": schedules_view,
                "open_alerts": [
                    {"id": a["id"], "type": a["type"], "severity": a["severity"], "state": a["state"]}
                    for a in open_alerts
                ],
            }
        )
    return out
