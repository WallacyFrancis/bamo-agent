"""Agendamento local opt-in de capacidades `read`/`notify` (PRD-004, seções
4 e 6, decisão 11.1).

Cada agenda é um arquivo `schedules/schedule-*.json` (0600, diretório 0700,
ignorado pelo Git). `bamo scheduler run` — disparado pelo cron do sistema;
o Bamo nunca instala/edita crontab — processa, numa única execução, as
agendas devidas agora, sob um lock de arquivo que impede sobreposição
(seção 8.6). O processo de agenda não lê prompts, chat nem conversa; chama
o dispatcher só com o `connector_id` já persistido.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import connector_audit, connectors, dispatcher, ids
from .atomic import ensure_dir_secure, write_bytes_atomic_secure
from .paths import SCHEDULES_DIR

SCHEMA_VERSION = 1

# Intervalo mínimo aprovado (seção 8.6: "recusar schedules abaixo do
# intervalo mínimo aprovado") — evita agendas tão frequentes que viram, na
# prática, um laço sem controle de taxa.
MIN_INTERVAL_MINUTES = 5

LOCK_PATH = SCHEDULES_DIR / ".scheduler.lock"


class SchedulerError(Exception):
    pass


class ScheduleNotFoundError(SchedulerError):
    pass


class ScheduleExistsError(SchedulerError):
    pass


class InvalidIntervalError(SchedulerError):
    pass


class NotSchedulableError(SchedulerError):
    pass


class SchedulerBusyError(SchedulerError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def path_for(schedule_id: str) -> Path:
    ids.validate_id(schedule_id)
    return SCHEDULES_DIR / f"{schedule_id}.json"


_REQUIRED_STR_FIELDS = ("id", "connector_id", "capability", "created_at", "updated_at")
_DATE_FIELDS = ("created_at", "updated_at")

# Tolerância de relógio para datas gravadas pelo próprio Bamo (created_at,
# updated_at, last_run_at) — tudo isso é sempre `datetime.now(timezone.utc)`
# no momento da escrita, nunca informado por fora. Uma data claramente no
# futuro além dessa folga é sinal de adulteração ou corrupção, não de skew
# normal de relógio local.
_MAX_FUTURE_SKEW = timedelta(minutes=5)


def _parse_dt(value: Any) -> datetime:
    """Único ponto que decide se uma data de agenda é válida: string,
    ISO-8601 parseável, com timezone explícito e não no futuro além da
    tolerância. Levanta `ValueError` limpo em qualquer desvio — nunca deixa
    `datetime.fromisoformat` propagar cru para quem chamou."""
    if not isinstance(value, str) or not value:
        raise ValueError("data ausente ou não é string")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"data em formato ISO-8601 inválido: {value!r}") from exc
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"data sem timezone: {value!r}")
    if dt > _now() + _MAX_FUTURE_SKEW:
        raise ValueError(f"data no futuro além da tolerância permitida: {value!r}")
    return dt


def _validate_schedule(data: Any) -> dict[str, Any]:
    """Espelha `_validate_connector`/`_validate_envelope`: um
    `schedule-*.json` sintaticamente válido mas estruturalmente errado
    sempre vira SchedulerError aqui, nunca KeyError/TypeError/ValueError cru
    — inclui toda data do registro (`created_at`, `updated_at`,
    `last_run_at`), validada via `_parse_dt` antes de `_is_due()` jamais
    tocar `datetime.fromisoformat` sobre um valor não confiável."""
    if not isinstance(data, dict):
        raise SchedulerError(f"registro de agenda não é um objeto JSON (recebido {type(data).__name__})")
    for field in _REQUIRED_STR_FIELDS:
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise SchedulerError(f"registro de agenda com campo ausente ou inválido: {field!r}")

    # `id` e `connector_id` precisam ser IDs gerados pelo Bamo, não só
    # strings não vazias — sem isso, um arquivo adulterado poderia propagar
    # um ID malformado (ex.: path traversal) para `path_for`, `_mark_run`,
    # o dispatcher ou a auditoria mais adiante.
    try:
        ids.validate_id(data["id"])
    except ids.InvalidIdError as exc:
        raise SchedulerError(f"registro de agenda com id malformado: {data['id']!r}") from exc
    try:
        ids.validate_id(data["connector_id"])
    except ids.InvalidIdError as exc:
        raise SchedulerError(f"registro de agenda com connector_id malformado: {data['connector_id']!r}") from exc

    # `capability` só é válida se o conector referenciado existir, estiver
    # íntegro, tiver um provider conhecido e declarar essa capacidade tanto
    # na allowlist do provider quanto no próprio registro do conector —
    # nenhuma dessas checagens pode ser pulada antes de qualquer execução,
    # marcação de `last_run_at` ou entrada de auditoria.
    try:
        connector = connectors.load(data["connector_id"])
    except connectors.ConnectorError as exc:
        raise SchedulerError(f"registro de agenda referencia conector inválido: {exc}") from exc
    if connector is None:
        raise SchedulerError(f"registro de agenda referencia conector inexistente: {data['connector_id']!r}")
    provider = connectors.PROVIDERS.get(connector["provider"])
    if provider is None:
        raise SchedulerError(f"registro de agenda referencia conector com provider desconhecido: {connector['provider']!r}")
    capability = data["capability"]
    if capability not in provider["capabilities"] or capability not in connector["capabilities"]:
        raise SchedulerError(f"registro de agenda com capacidade inválida para o conector referenciado: {capability!r}")

    for field in _DATE_FIELDS:
        try:
            _parse_dt(data[field])
        except ValueError as exc:
            raise SchedulerError(f"registro de agenda com {field!r} inválido: {exc}") from exc
    if not isinstance(data.get("enabled"), bool):
        raise SchedulerError("registro de agenda com 'enabled' ausente ou inválido")
    every = data.get("every_minutes")
    if not isinstance(every, int) or isinstance(every, bool) or every < MIN_INTERVAL_MINUTES:
        raise SchedulerError("registro de agenda com 'every_minutes' ausente ou inválido")
    last_run_at = data.get("last_run_at")
    if last_run_at is not None:
        try:
            _parse_dt(last_run_at)
        except ValueError as exc:
            raise SchedulerError(f"registro de agenda com 'last_run_at' inválido: {exc}") from exc
    last_status = data.get("last_status")
    if last_status is not None and not isinstance(last_status, str):
        raise SchedulerError("registro de agenda com 'last_status' inválido")
    return data


def _save(record: dict[str, Any]) -> None:
    data = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    ensure_dir_secure(SCHEDULES_DIR)
    write_bytes_atomic_secure(path_for(record["id"]), data)


def load(schedule_id: str) -> dict[str, Any] | None:
    path = path_for(schedule_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchedulerError("registro de agenda corrompido ou ilegível") from exc
    return _validate_schedule(data)


def list_all() -> list[dict[str, Any]]:
    if not SCHEDULES_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(SCHEDULES_DIR.glob("sched-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SchedulerError(f"registro de agenda corrompido ou ilegível: {path.name}") from exc
        out.append(_validate_schedule(data))
    return sorted(out, key=lambda s: s["created_at"], reverse=True)


def set_schedule(connector_id: str, capability: str, every_minutes: int) -> dict[str, Any]:
    connector = connectors.load(connector_id)
    if connector is None:
        raise ScheduleNotFoundError(f"conector não encontrado: {connector_id}")

    provider = connectors.PROVIDERS.get(connector["provider"])
    cap_def = provider["capabilities"].get(capability) if provider else None
    if cap_def is None or capability not in connector["capabilities"]:
        raise NotSchedulableError(f"capacidade não habilitada para este conector: {capability!r}")
    # Só capacidades read/notify, idempotentes e sem efeito externo de
    # escrita, podem ser agendadas nesta fase (seção 6, última observação).
    if cap_def["kind"] not in ("read", "notify") or not cap_def.get("schedulable"):
        raise NotSchedulableError(f"capacidade não agendável (efeito de escrita ou não idempotente): {capability!r}")

    if not isinstance(every_minutes, int) or isinstance(every_minutes, bool) or every_minutes < MIN_INTERVAL_MINUTES:
        raise InvalidIntervalError(f"intervalo mínimo aprovado é {MIN_INTERVAL_MINUTES} minutos")

    for existing in list_all():
        if existing["connector_id"] == connector_id and existing["capability"] == capability and existing["enabled"]:
            raise ScheduleExistsError(f"já existe uma agenda ativa para {connector_id}/{capability}: {existing['id']}")

    schedule_id = ids.new_id("sched")
    now = _now_iso()
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": schedule_id,
        "connector_id": connector_id,
        "capability": capability,
        "every_minutes": every_minutes,
        "enabled": True,
        "last_run_at": None,
        "last_status": None,
        "created_at": now,
        "updated_at": now,
    }
    _save(record)
    return record


def disable(schedule_id: str) -> bool:
    record = load(schedule_id)
    if record is None:
        return False
    record["enabled"] = False
    record["updated_at"] = _now_iso()
    _save(record)
    return True


def delete_for_connector(connector_id: str) -> int:
    """Usado por `bamo connector delete` para apagar agendas associadas
    (seção 7: "Apagar um conector remove sua configuração e agendas
    associadas"). Fica aqui (não em `connectors.delete`) para evitar
    import circular entre os dois módulos."""
    removed = 0
    for record in list_all():
        if record["connector_id"] == connector_id:
            path_for(record["id"]).unlink(missing_ok=True)
            removed += 1
    return removed


def _is_due(record: dict[str, Any], now: datetime) -> bool:
    """`list_all()`/`load()` já validam `last_run_at`/`every_minutes` via
    `_validate_schedule()` antes de um registro chegar aqui; o try/except
    abaixo é uma rede de segurança extra (mesmo padrão de `_unlock()` no
    cofre, PRD-003) para que nenhum `ValueError`/`TypeError`/`KeyError`
    escape de `bamo scheduler run` caso algo escape da validação
    centralizada. Um registro que não dá para avaliar com segurança é
    tratado como "não devido agora" — nunca dispara execução sobre um
    estado que o Bamo não conseguiu confirmar."""
    try:
        last_run_at = record["last_run_at"]
        if last_run_at is None:
            return True
        last_run = _parse_dt(last_run_at)
        every_minutes = record["every_minutes"]
        if not isinstance(every_minutes, int) or isinstance(every_minutes, bool):
            return False
        return now >= last_run + timedelta(minutes=every_minutes)
    except (ValueError, TypeError, KeyError):
        return False


def _mark_run(record: dict[str, Any], now: datetime, status: str) -> None:
    record["last_run_at"] = now.isoformat()
    record["last_status"] = status
    record["updated_at"] = now.isoformat()
    _save(record)


@contextmanager
def _lock() -> Iterator[None]:
    ensure_dir_secure(SCHEDULES_DIR)
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise SchedulerBusyError("já existe uma execução do agendador em andamento") from exc
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass


def run_due() -> list[dict[str, Any]]:
    """Executa, uma única vez, todas as agendas habilitadas e devidas agora
    — chamado por `bamo scheduler run`. Uma falha em uma agenda não
    interrompe as demais nem causa nova tentativa dentro desta mesma
    execução (seção 8.6: "uma falha não deve causar repetição em loop");
    cada disparo do cron é uma execução independente."""
    now = _now()
    results: list[dict[str, Any]] = []
    with _lock():
        for record in list_all():
            if not record["enabled"] or not _is_due(record, now):
                continue

            started_at = _now_iso()
            try:
                connector = connectors.load(record["connector_id"])
            except connectors.ConnectorError:
                connector = None
            if connector is None or not connector["enabled"]:
                status = "connector_unavailable"
                _mark_run(record, now, status)
                connector_audit.append_event(
                    connector_id=record["connector_id"],
                    capability=record["capability"],
                    origin="schedule",
                    started_at=started_at,
                    ended_at=_now_iso(),
                    ok=False,
                    status=status,
                )
                results.append({"schedule_id": record["id"], "ok": False, "status": status})
                continue

            try:
                outcome = dispatcher.run_capability(connector, record["capability"])
                ok, status, summary = outcome["ok"], outcome["status"], outcome["summary"]
            except dispatcher.DispatcherError as exc:
                ok, status, summary = False, exc.__class__.__name__, str(exc)

            ended_at = _now_iso()
            _mark_run(record, now, status)
            connector_audit.append_event(
                connector_id=record["connector_id"],
                capability=record["capability"],
                origin="schedule",
                started_at=started_at,
                ended_at=ended_at,
                ok=ok,
                status=status,
                summary=summary,
            )
            results.append({"schedule_id": record["id"], "ok": ok, "status": status})
    return results
