"""Caixa de alertas operacionais local (PRD-006, seções 4 e 5).

Um alerta (`alerts/<id>.json`, 0600, diretório 0700, ignorado pelo Git) é
derivado só de eventos já gravados em `connector-audit/` (PRD-004/005) —
este módulo nunca abre o cofre, nunca importa executor algum, nunca chama
`agy` e não tem rede (PRD-006, seção 7.1). Ele guarda só metadados de
classificação: conector, agenda (quando houver), capacidade, tipo/gravidade
allowlisted, estado, contador e uma mensagem curta já redigida — nunca
`secret_id`, rótulo/valor de segredo, payload de API ou URL (seção 5.1).

Cada alerta é deduplicado por `(connector_id, capability, kind)`: `kind` é
a "linhagem" estável usada para localizar o registro existente
(`_find_existing`), enquanto `type` é o rótulo específico mostrado ao
usuário e pode evoluir dentro da mesma linhagem (`execution_failed` ->
`repeated_failure` após 3 falhas consecutivas) sem criar um segundo
arquivo — só assim "atualizações incrementam contador e last_seen_at, sem
criar um arquivo por execução" (seção 5.2).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import ids
from .atomic import ensure_dir_secure, write_bytes_atomic_secure
from .paths import ALERTS_DIR
from .redact import redact

SCHEMA_VERSION = 1
MAX_MESSAGE_CHARS = 2000
MIN_MUTE_MINUTES = 5
MAX_MUTE_MINUTES = 7 * 24 * 60
RETENTION_DAYS = 30
REPEATED_FAILURE_THRESHOLD = 3

_STATES = ("open", "acknowledged", "muted")

# Tabela de classificação (PRD-006, seção 5.2). `kind` é a chave de
# deduplicação/lookup; qualquer status de falha fora deste mapa cai em
# "failure" (linhagem execution_failed -> repeated_failure).
_STATUS_KIND = {
    "secret_unavailable": "credential_unavailable",
    "unauthorized": "access_denied",
    "forbidden": "access_denied",
    "rate_limited": "rate_limited",
    "connector_unavailable": "connector_unavailable",
}
_KIND_SEVERITY = {
    "credential_unavailable": "error",
    "access_denied": "error",
    "rate_limited": "warning",
    "connector_unavailable": "warning",
}
_KIND_DEFAULT_TYPE = {
    "credential_unavailable": "credential_unavailable",
    "access_denied": "access_denied",
    "rate_limited": "rate_limited",
    "connector_unavailable": "connector_unavailable",
    "failure": "execution_failed",
}
_VALID_KINDS = frozenset(_KIND_DEFAULT_TYPE)
_VALID_TYPES = frozenset(_KIND_DEFAULT_TYPE.values()) | {"repeated_failure"}

# Só a linhagem "failure" é transitória (seção 5.2: "Sucesso encerra
# somente alertas transitórios") — as demais exigem revisão humana
# explícita (acknowledge/mute/unmute), mesmo depois de uma execução ok.
_TRANSIENT_KIND = "failure"


class AlertError(Exception):
    pass


class AlertNotFoundError(AlertError):
    pass


class AlertCorruptedError(AlertError):
    pass


class InvalidMuteDurationError(AlertError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def path_for(alert_id: str) -> Path:
    ids.validate_id(alert_id)
    return ALERTS_DIR / f"{alert_id}.json"


_REQUIRED_STR_FIELDS = ("id", "kind", "type", "severity", "connector_id", "capability", "state", "created_at", "updated_at", "last_seen_at")
_MAX_FUTURE_SKEW = timedelta(minutes=5)


_MAX_MUTE_SKEW = timedelta(minutes=MAX_MUTE_MINUTES + 5)


def _parse_dt(value: Any, max_future: timedelta = _MAX_FUTURE_SKEW) -> datetime:
    """`max_future` limita o quão à frente do relógio atual a data pode
    estar. Para `created_at`/`updated_at`/`last_seen_at` (sempre `now()` no
    momento da escrita) o padrão (`_MAX_FUTURE_SKEW`, poucos minutos) é uma
    checagem de adulteração; `muted_until` é um prazo futuro intencional de
    até `MAX_MUTE_MINUTES` (7 dias) e precisa de um limite bem maior — sem
    isso, todo `mute` realista seria rejeitado como "data no futuro"."""
    if not isinstance(value, str) or not value:
        raise ValueError("data ausente ou não é string")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"data em formato ISO-8601 inválido: {value!r}") from exc
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"data sem timezone: {value!r}")
    if dt > _now() + max_future:
        raise ValueError(f"data no futuro além da tolerância permitida: {value!r}")
    return dt


def _validate_alert(data: Any) -> dict[str, Any]:
    """Espelha `_validate_connector`/`_validate_schedule`: um
    `alerts/alert-*.json` sintaticamente válido mas estruturalmente errado
    sempre vira AlertCorruptedError aqui, nunca KeyError/TypeError/ValueError
    cru — um item corrompido nunca impede os demais de aparecer (seção 7.4,
    diferente do padrão fail-closed de conector/agenda: aqui é só estado
    operacional derivado, não algo que autoriza execução)."""
    if not isinstance(data, dict):
        raise AlertCorruptedError(f"alerta não é um objeto JSON (recebido {type(data).__name__})")
    for field in _REQUIRED_STR_FIELDS:
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise AlertCorruptedError(f"alerta com campo ausente ou inválido: {field!r}")

    for field in ("id", "connector_id"):
        try:
            ids.validate_id(data[field])
        except ids.InvalidIdError as exc:
            raise AlertCorruptedError(f"alerta com {field} malformado: {data[field]!r}") from exc

    schedule_id = data.get("schedule_id")
    if schedule_id is not None:
        if not isinstance(schedule_id, str):
            raise AlertCorruptedError("alerta com schedule_id inválido")
        try:
            ids.validate_id(schedule_id)
        except ids.InvalidIdError as exc:
            raise AlertCorruptedError("alerta com schedule_id malformado") from exc

    if data["kind"] not in _VALID_KINDS:
        raise AlertCorruptedError(f"alerta com kind desconhecido: {data['kind']!r}")
    if data["type"] not in _VALID_TYPES:
        raise AlertCorruptedError(f"alerta com type desconhecido: {data['type']!r}")
    if data["severity"] not in ("warning", "error"):
        raise AlertCorruptedError(f"alerta com severity desconhecida: {data['severity']!r}")
    if data["state"] not in _STATES:
        raise AlertCorruptedError(f"alerta com state desconhecido: {data['state']!r}")

    count = data.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise AlertCorruptedError("alerta com 'count' ausente ou inválido")

    for field in ("created_at", "updated_at", "last_seen_at"):
        try:
            _parse_dt(data[field])
        except ValueError as exc:
            raise AlertCorruptedError(f"alerta com {field!r} inválido: {exc}") from exc

    muted_until = data.get("muted_until")
    if muted_until is not None:
        try:
            _parse_dt(muted_until, max_future=_MAX_MUTE_SKEW)
        except ValueError as exc:
            raise AlertCorruptedError(f"alerta com 'muted_until' inválido: {exc}") from exc

    acknowledged_at = data.get("acknowledged_at")
    if acknowledged_at is not None:
        try:
            _parse_dt(acknowledged_at)
        except ValueError as exc:
            raise AlertCorruptedError(f"alerta com 'acknowledged_at' inválido: {exc}") from exc

    message = data.get("message")
    if not isinstance(message, str):
        raise AlertCorruptedError("alerta com 'message' inválida")

    return data


def _save(record: dict[str, Any]) -> None:
    data = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    ensure_dir_secure(ALERTS_DIR)
    write_bytes_atomic_secure(path_for(record["id"]), data)


def load(alert_id: str) -> dict[str, Any] | None:
    path = path_for(alert_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AlertCorruptedError("alerta corrompido ou ilegível") from exc
    record = _validate_alert(data)
    return _expire_mute_if_due(record)


def list_all(connector_id: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
    """Item corrompido é pulado, nunca derruba a listagem inteira (seção
    7.4: "item operacional malformado não produz traceback nem bloqueia
    dados válidos") — diferente de conector/agenda, cujo fail-closed
    protege uma decisão de execução; aqui é só visão operacional."""
    if not ALERTS_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(ALERTS_DIR.glob("alert-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            record = _validate_alert(data)
        except (OSError, json.JSONDecodeError, AlertCorruptedError):
            continue
        record = _expire_mute_if_due(record)
        if connector_id and record["connector_id"] != connector_id:
            continue
        if state and record["state"] != state:
            continue
        out.append(record)
    return sorted(out, key=lambda a: a["last_seen_at"], reverse=True)


def _expire_mute_if_due(record: dict[str, Any]) -> dict[str, Any]:
    """Chamado por `load()`/`list_all()` e antes de aplicar um novo evento
    em `record_event()`: um `mute` vencido é persistido em disco como
    `open` (com `muted_until` limpo e `updated_at` atualizado) — nunca só
    devolvido "de mentira" para exibição, senão `alert list`, `alert show`
    e `status` divergiriam do arquivo (seção 6: "Ao vencer, volta a open
    se a condição persistir"). Essa transição é só de estado: não gera
    execução, notificação externa nem alteração de agenda."""
    if record["state"] != "muted":
        return record
    muted_until = record.get("muted_until")
    if muted_until is None:
        return record
    try:
        expired = _now() >= _parse_dt(muted_until, max_future=_MAX_MUTE_SKEW)
    except ValueError:
        return record
    if not expired:
        return record
    record = dict(record)
    record["state"] = "open"
    record["muted_until"] = None
    record["updated_at"] = _now_iso()
    _save(record)
    return record


def _find_existing(connector_id: str, capability: str, kind: str, schedule_id: str | None) -> dict[str, Any] | None:
    """Deduplicação por conector, agenda (quando houver), capacidade e
    linhagem/tipo (seção 5.2): duas agendas diferentes do mesmo conector e
    capacidade, ou uma execução manual (`schedule_id=None`) e uma agendada,
    nunca compartilham o mesmo alerta."""
    for record in list_all(connector_id=connector_id):
        if record["capability"] == capability and record["kind"] == kind and record.get("schedule_id") == schedule_id:
            return record
    return None


def _close_failure_alert(connector_id: str, capability: str, schedule_id: str | None, ended_at: str) -> None:
    existing = _find_existing(connector_id, capability, _TRANSIENT_KIND, schedule_id)
    if existing is None:
        return
    path_for(existing["id"]).unlink(missing_ok=True)


def record_event(
    *,
    connector_id: str,
    capability: str,
    schedule_id: str | None,
    origin: str,
    ok: bool,
    status: str,
    ended_at: str,
) -> tuple[dict[str, Any] | None, bool]:
    """Classifica um evento já gravado em `connector-audit/` (seção 5.2) e
    cria/atualiza/encerra o alerta correspondente. Retorna
    `(alerta_ou_None, deve_anunciar)`: `deve_anunciar` é True só para um
    alerta novo ou que acabou de escalar de tipo (seção 3.4: "notificar
    falha nova, mudança relevante ou falha repetida; não repetir alertas
    idênticos a cada ciclo") — nunca para uma atualização silenciosa de
    contador."""
    ids.validate_id(connector_id)
    if schedule_id is not None:
        ids.validate_id(schedule_id)

    if ok:
        _close_failure_alert(connector_id, capability, schedule_id, ended_at)
        return None, False

    kind = _STATUS_KIND.get(status, _TRANSIENT_KIND)
    existing = _find_existing(connector_id, capability, kind, schedule_id)

    if existing is None:
        alert_id = ids.new_id("alert")
        now = ended_at
        message, _ = redact(f"status={status} origem={origin}"[:MAX_MESSAGE_CHARS])
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "id": alert_id,
            "kind": kind,
            "type": _KIND_DEFAULT_TYPE[kind],
            "severity": _severity_for(kind, _KIND_DEFAULT_TYPE[kind]),
            "connector_id": connector_id,
            "schedule_id": schedule_id,
            "capability": capability,
            "state": "open",
            "count": 1,
            "message": message,
            "created_at": now,
            "updated_at": now,
            "last_seen_at": now,
            "muted_until": None,
            "acknowledged_at": None,
        }
        _save(record)
        return record, True

    record = _expire_mute_if_due(dict(existing))
    previous_type = record["type"]
    record["count"] = record["count"] + 1
    # `schedule_id` já faz parte da chave de dedup usada por `_find_existing`
    # acima — este é sempre o mesmo valor já gravado no registro, não uma
    # sobrescrita de agenda diferente.
    record["updated_at"] = ended_at
    record["last_seen_at"] = ended_at
    message, _ = redact(f"status={status} origem={origin}"[:MAX_MESSAGE_CHARS])
    record["message"] = message

    if kind == _TRANSIENT_KIND and record["count"] >= REPEATED_FAILURE_THRESHOLD:
        record["type"] = "repeated_failure"
    record["severity"] = _severity_for(kind, record["type"])

    escalated = record["type"] != previous_type
    if escalated:
        # Mudança relevante (seção 3.4) sempre volta a exigir revisão
        # humana, mesmo que a versão anterior do alerta já tivesse sido
        # reconhecida ou silenciada.
        record["state"] = "open"
        record["muted_until"] = None

    _save(record)
    return record, escalated


def _severity_for(kind: str, type_: str) -> str:
    if kind == _TRANSIENT_KIND:
        return "error" if type_ == "repeated_failure" else "warning"
    return _KIND_SEVERITY[kind]


def acknowledge(alert_id: str) -> dict[str, Any]:
    record = load(alert_id)
    if record is None:
        raise AlertNotFoundError(f"alerta não encontrado: {alert_id}")
    record["state"] = "acknowledged"
    record["acknowledged_at"] = _now_iso()
    record["updated_at"] = _now_iso()
    _save(record)
    return record


def mute(alert_id: str, minutes: int) -> dict[str, Any]:
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not (MIN_MUTE_MINUTES <= minutes <= MAX_MUTE_MINUTES):
        raise InvalidMuteDurationError(
            f"duração de silenciamento precisa estar entre {MIN_MUTE_MINUTES} e {MAX_MUTE_MINUTES} minutos"
        )
    record = load(alert_id)
    if record is None:
        raise AlertNotFoundError(f"alerta não encontrado: {alert_id}")
    now = _now()
    record["state"] = "muted"
    record["muted_until"] = (now + timedelta(minutes=minutes)).isoformat()
    record["updated_at"] = now.isoformat()
    _save(record)
    return record


def unmute(alert_id: str) -> dict[str, Any]:
    record = load(alert_id)
    if record is None:
        raise AlertNotFoundError(f"alerta não encontrado: {alert_id}")
    record["state"] = "open"
    record["muted_until"] = None
    record["updated_at"] = _now_iso()
    _save(record)
    return record


def cleanup_expired(retention_days: int = RETENTION_DAYS) -> int:
    """Remove só alertas sem nenhuma atividade há mais de `retention_days`
    — nunca cofre, conector, agenda, auditoria-base, sessão, memória ou OKF
    (seção 7.7)."""
    if not ALERTS_DIR.exists():
        return 0
    cutoff = _now() - timedelta(days=retention_days)
    removed = 0
    for path in ALERTS_DIR.glob("alert-*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            record = _validate_alert(data)
            last_seen = _parse_dt(record["last_seen_at"])
        except (OSError, json.JSONDecodeError, AlertCorruptedError, ValueError):
            continue
        if last_seen < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
