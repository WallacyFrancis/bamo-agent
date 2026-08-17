"""Registro local de conectores com capacidades explícitas (PRD-004).

Isolamento deliberado, no mesmo espírito do cofre (PRD-003, ver
`core/vault.py`): este módulo só guarda metadados de conector (id,
provider, capacidades habilitadas, referência opcional a um secret-id do
cofre). Nenhum valor de segredo, cookie, token ou payload de resposta
remota é gravado aqui — isso é responsabilidade do dispatcher/executor
(`core/dispatcher.py`), que nunca escreve neste diretório.

Formato em disco (`connectors/<connector-id>.json`, um JSON por conector,
0600, diretório 0700, ignorado pelo Git — igual ao cofre):

    {
        "schema_version": 1,
        "id": "conn-...",
        "provider": "local-demo",
        "display_name": "...",
        "enabled": true,
        "capabilities": ["read_status", "notify_summary"],
        "secret_id": null,
        "allowed_origins": ["local-demo"],
        "schedule": null,
        "created_at": <iso>, "updated_at": <iso>,
    }

`provider`, capacidades e origens pertencem à allowlist `PROVIDERS` abaixo,
implementada em código — nunca a texto livre enviado pelo usuário (PRD-004,
seção 5.1). O campo `schedule` é reservado pelo schema documentado no PRD e
permanece sempre `null` nesta fase: a fonte de verdade de agendamento é o
diretório `schedules/`, gerido por `core/scheduler.py`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ids
from .atomic import ensure_dir_secure, write_bytes_atomic_secure
from .paths import CONNECTORS_DIR, EXECUTORS_DIR

SCHEMA_VERSION = 1

# Allowlist implementada de providers/capacidades (PRD-004, seção 5.1 e
# 8.7): o que um conector pode fazer nunca vem de texto enviado pelo
# usuário, só desta tabela fixa no código. Novo provider, capacidade de
# escrita ou permissão de rede exige PRD posterior — não basta editar um
# registro em disco.
PROVIDERS: dict[str, dict[str, Any]] = {
    "local-demo": {
        "display_name": "Demonstração local",
        "executor_path": EXECUTORS_DIR / "local_demo.py",
        "allowed_origins": ["local-demo"],
        "capabilities": {
            "read_status": {
                "kind": "read",
                "requires_secret": False,
                "schedulable": True,
                "description": "Lê status sintético local (sem rede, sem segredo real).",
            },
            "notify_summary": {
                "kind": "notify",
                "requires_secret": False,
                "schedulable": True,
                "description": "Gera um resumo sintético local (sem envio externo).",
            },
        },
    }
}


class ConnectorError(Exception):
    pass


class ConnectorNotFoundError(ConnectorError):
    pass


class ConnectorCorruptedError(ConnectorError):
    pass


InvalidConnectorIdError = ids.InvalidIdError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def path_for(connector_id: str) -> Path:
    ids.validate_id(connector_id)
    return CONNECTORS_DIR / f"{connector_id}.json"


_REQUIRED_STR_FIELDS = ("id", "provider", "display_name", "created_at", "updated_at")


def _validate_connector(data: Any) -> dict[str, Any]:
    """Único ponto que decide se um `connectors/conn-*.json` decodificado
    tem o formato esperado — espelha `_validate_envelope` do cofre
    (PRD-003). JSON sintaticamente válido mas estruturalmente errado
    (provider desconhecido, capacidade fora da allowlist, tipos trocados)
    sempre vira ConnectorCorruptedError aqui, nunca KeyError/TypeError cru
    para quem chamou (PRD-004, seção 9)."""
    if not isinstance(data, dict):
        raise ConnectorCorruptedError(f"registro de conector não é um objeto JSON (recebido {type(data).__name__})")

    for field in _REQUIRED_STR_FIELDS:
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise ConnectorCorruptedError(f"registro de conector com campo ausente ou inválido: {field!r}")

    # O `id` interno do registro precisa ser um ID gerado pelo Bamo, não só
    # uma string não vazia — sem isso, um arquivo adulterado poderia
    # propagar um ID malformado (ex.: path traversal) para o dispatcher, a
    # auditoria ou `_save`/`path_for` mais adiante.
    try:
        ids.validate_id(data["id"])
    except ids.InvalidIdError as exc:
        raise ConnectorCorruptedError(f"registro de conector com id malformado: {data['id']!r}") from exc

    provider = PROVIDERS.get(data["provider"])
    if provider is None:
        raise ConnectorCorruptedError(f"registro de conector com provider desconhecido: {data['provider']!r}")

    if not isinstance(data.get("enabled"), bool):
        raise ConnectorCorruptedError("registro de conector com 'enabled' ausente ou inválido")

    caps = data.get("capabilities")
    if not isinstance(caps, list) or not caps or not all(isinstance(c, str) for c in caps):
        raise ConnectorCorruptedError("registro de conector com 'capabilities' ausente ou inválida")
    unknown = [c for c in caps if c not in provider["capabilities"]]
    if unknown:
        raise ConnectorCorruptedError(f"registro de conector com capacidades desconhecidas: {unknown!r}")

    secret_id = data.get("secret_id")
    if secret_id is not None:
        if not isinstance(secret_id, str):
            raise ConnectorCorruptedError("registro de conector com secret_id inválido")
        try:
            ids.validate_id(secret_id)
        except ids.InvalidIdError as exc:
            raise ConnectorCorruptedError("registro de conector com secret_id malformado") from exc

    origins = data.get("allowed_origins")
    if not isinstance(origins, list) or not origins or not all(isinstance(o, str) for o in origins):
        raise ConnectorCorruptedError("registro de conector com 'allowed_origins' ausente ou inválida")
    # `allowed_origins` pertence à allowlist do provider (seção 5.1) — não
    # basta ser uma lista de strings, precisa corresponder exatamente ao que
    # o provider declara. Sem isso, um registro adulterado com uma origem
    # estranha (ex.: "https://evil.example") passaria despercebido.
    if sorted(origins) != sorted(provider["allowed_origins"]):
        raise ConnectorCorruptedError(
            f"registro de conector com 'allowed_origins' divergente da allowlist do provider: {origins!r}"
        )

    return data


def _save(record: dict[str, Any]) -> None:
    data = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    ensure_dir_secure(CONNECTORS_DIR)
    write_bytes_atomic_secure(path_for(record["id"]), data)


def create_demo(display_name: str) -> dict[str, Any]:
    provider_name = "local-demo"
    provider = PROVIDERS[provider_name]
    label = ids.validate_label(display_name)

    connector_id = ids.new_id("conn")
    now = _now()
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": connector_id,
        "provider": provider_name,
        "display_name": label,
        "enabled": True,
        "capabilities": sorted(provider["capabilities"].keys()),
        "secret_id": None,
        "allowed_origins": list(provider["allowed_origins"]),
        "schedule": None,
        "created_at": now,
        "updated_at": now,
    }
    _save(record)
    return record


def load(connector_id: str) -> dict[str, Any] | None:
    path = path_for(connector_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConnectorCorruptedError("registro de conector corrompido ou ilegível") from exc
    return _validate_connector(data)


def list_all() -> list[dict[str, Any]]:
    if not CONNECTORS_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(CONNECTORS_DIR.glob("conn-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConnectorCorruptedError(f"registro de conector corrompido ou ilegível: {path.name}") from exc
        out.append(_validate_connector(data))
    return sorted(out, key=lambda c: c["created_at"], reverse=True)


def _set_enabled(connector_id: str, enabled: bool) -> dict[str, Any]:
    record = load(connector_id)
    if record is None:
        raise ConnectorNotFoundError(f"conector não encontrado: {connector_id}")
    record["enabled"] = enabled
    record["updated_at"] = _now()
    _save(record)
    return record


def enable(connector_id: str) -> dict[str, Any]:
    return _set_enabled(connector_id, True)


def disable(connector_id: str) -> dict[str, Any]:
    return _set_enabled(connector_id, False)


def delete(connector_id: str) -> bool:
    """Confirmação de `--confirm` é responsabilidade do chamador (CLI),
    igual ao padrão já usado em memory/session/knowledge/secret. Agendas
    associadas são removidas pelo chamador via
    `scheduler.delete_for_connector` (evita import circular aqui)."""
    path = path_for(connector_id)
    if not path.exists():
        return False
    path.unlink()
    return True
