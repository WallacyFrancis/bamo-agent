"""Dispatcher: ponte entre o registro de conectores (`core/connectors.py`)
e o executor isolado de cada provider (PRD-004, seção 5.2).

Único ponto do Bamo que inicia um processo executor. Nunca chamado a partir
de `chat`/`ask`/`agy_runtime` — só pela CLI (`bamo connector run`) e pelo
agendador (`core/scheduler.py`). O `agy` nunca vê este módulo nem seu
resultado bruto (PRD-004, seção 2).

Contrato com o executor (seção 5.2): processo filho via `subprocess`, sem
shell, argumentos fixos, diretório de trabalho controlado (o próprio
diretório do executor), timeout e tamanho máximo de saída obrigatórios;
stdin carrega um pedido JSON pequeno, stdout devolve uma única linha JSON;
stderr é descartado (seção 8.3 — nunca persistido bruto). Qualquer desvio
desse contrato (executor ausente, timeout, saída grande demais, saída fora
do schema) vira uma subclasse de `DispatcherError` — nunca uma exceção
crua de `subprocess`/`json` escapa para o chamador (seção 9).
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
from typing import Any

from .connectors import PROVIDERS
from .redact import redact

EXECUTOR_TIMEOUT_SECONDS = 10
MAX_OUTPUT_BYTES = 8192
MAX_TEXT_CHARS = 2000


class DispatcherError(Exception):
    pass


class ConnectorDisabledError(DispatcherError):
    pass


class UnknownCapabilityError(DispatcherError):
    pass


class SecretUnavailableError(DispatcherError):
    pass


class ExecutorMissingError(DispatcherError):
    pass


class ExecutorTimeoutError(DispatcherError):
    pass


class ExecutorOutputTooLargeError(DispatcherError):
    pass


class ExecutorProtocolError(DispatcherError):
    pass


def _child_env(requires_secret: bool) -> dict[str, str]:
    """Ambiente mínimo do executor — nunca herda o ambiente completo do
    processo do Bamo (seção 8: nenhuma variável de ambiente com segredo
    pode alcançar o executor). Quando a capacidade exige credencial, o
    executor precisa desbloquear o cofre no próprio processo (PRD-005,
    seção 5.2) — o que pode depender do keyring do sistema (barramento
    D-Bus/serviço de sessão). Só nesse caso repassamos, além de PATH/LANG,
    as variáveis não sensíveis de localização de sessão que o keyring
    precisa; nenhuma delas carrega segredo algum."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    if requires_secret:
        for var in ("HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            value = os.environ.get(var)
            if value:
                env[var] = value
    return env


def _read_capped(proc: subprocess.Popen, limit: int, deadline: float) -> tuple[bytes, bool, bool]:
    """Lê stdout do executor em pedaços, com teto de bytes e prazo de
    parede. Nunca acumula além de `limit` bytes na memória e nunca bloqueia
    além de `deadline` — condição exigida pela seção 8.2 (timeout e tamanho
    máximo de saída "obrigatórios"). Retorna (dados, estourou_limite,
    estourou_prazo)."""
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    output = bytearray()
    exceeded = False
    timed_out = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if not sel.select(timeout=min(remaining, 0.5)):
                continue
            chunk = proc.stdout.read(4096)
            if not chunk:
                break  # EOF: o executor fechou stdout (terminou de responder)
            output.extend(chunk)
            if len(output) > limit:
                exceeded = True
                break
    finally:
        sel.close()
    return bytes(output), exceeded, timed_out


def _terminate(proc: subprocess.Popen, own_process_group: bool) -> None:
    """Encerramento best-effort do processo filho — chamado sempre que o
    dispatcher retorna, com sucesso ou não, para nunca deixar um executor
    pendurado (seção 8.2).

    `own_process_group` reflete exatamente como `proc` foi criado em
    `run_capability()`: só chama `os.killpg(proc.pid, ...)` quando o
    executor foi de fato colocado em um grupo de processos próprio
    (`process_group=0`). Quando o executor compartilha o grupo do processo
    do Bamo/terminal (capacidades com `requires_secret`, para permitir o
    prompt de senha do cofre via `/dev/tty` — seção 5.2), `killpg` mataria
    o próprio Bamo e o terminal do usuário junto; nesse caso o encerramento
    é sempre por PID individual (`terminate`/`kill`)."""
    if proc.poll() is None:
        if own_process_group:
            try:
                os.killpg(proc.pid, 9)  # SIGKILL
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        else:
            try:
                proc.terminate()
            except OSError:
                pass
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _validate_response(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ExecutorProtocolError("resposta do executor não é um objeto JSON")
    ok = response.get("ok")
    status = response.get("status")
    summary = response.get("summary")
    safe_metadata = response.get("safe_metadata")
    if not isinstance(ok, bool) or not isinstance(status, str) or not status:
        raise ExecutorProtocolError("resposta do executor fora do schema esperado (campos 'ok'/'status')")
    if not isinstance(summary, str):
        raise ExecutorProtocolError("resposta do executor com 'summary' inválido")
    if not isinstance(safe_metadata, dict):
        raise ExecutorProtocolError("resposta do executor com 'safe_metadata' inválido")

    # Conteúdo devolvido pelo executor é dado não confiável (seção 3.6):
    # redige antes de qualquer uso e nunca o interpreta como instrução, só
    # como texto — mesma rede de segurança usada em sessão/memória/OKF.
    redacted_summary, _ = redact(summary[:MAX_TEXT_CHARS])
    try:
        safe_metadata_json = json.dumps(safe_metadata, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ExecutorProtocolError("resposta do executor com 'safe_metadata' não serializável") from exc
    if len(safe_metadata_json) > MAX_TEXT_CHARS:
        safe_metadata = {}

    return {"ok": ok, "status": status, "summary": redacted_summary, "safe_metadata": safe_metadata}


def run_capability(connector: dict[str, Any], capability: str) -> dict[str, Any]:
    """Executa uma capacidade de um conector já validado e habilitado pelo
    chamador (CLI ou agendador). Levanta `DispatcherError` (ou subclasse)
    em toda falha — nunca deixa uma exceção crua de subprocess/decodificação
    escapar (seção 9: "executor ausente, timeout, saída fora do schema ...
    falha sem traceback")."""
    if not connector.get("enabled"):
        raise ConnectorDisabledError(f"conector desabilitado: {connector['id']}")

    provider = PROVIDERS.get(connector["provider"])
    if provider is None:
        raise DispatcherError(f"provider desconhecido: {connector['provider']!r}")

    cap_def = provider["capabilities"].get(capability)
    if cap_def is None or capability not in connector.get("capabilities", []):
        raise UnknownCapabilityError(f"capacidade não habilitada para este conector: {capability!r}")

    requires_secret = bool(cap_def.get("requires_secret"))
    secret_id = connector.get("secret_id")
    if requires_secret and not secret_id:
        # A credencial pode ter sido apagada do conector (adulteração) sem
        # que o próprio conector tenha sido desabilitado — falha fechada em
        # vez de tentar executar sem credencial ou degradar para acesso
        # anônimo (PRD-005, seção 6).
        raise SecretUnavailableError(f"conector sem secret_id configurado para capacidade {capability!r}")

    executor_path = provider["executor_path"]
    if not executor_path.is_file():
        raise ExecutorMissingError(f"executor do provider ausente: {executor_path}")

    request = {
        "connector_id": connector["id"],
        "capability": capability,
        "params": {},
        "secret_id": secret_id,
    }
    request_bytes = json.dumps(request, ensure_ascii=False).encode("utf-8")

    argv = [sys.executable, "-I", str(executor_path)]
    popen_kwargs: dict[str, Any] = dict(
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=str(executor_path.parent),
        env=_child_env(requires_secret),
    )
    if not requires_secret:
        # Grupo de processos próprio, para permitir `os.killpg` limpo
        # (seção 8.2), sem usar start_new_session=True (que chamaria
        # setsid() e desconectaria o executor do terminal de controle).
        # Só é seguro quando a capacidade não precisa ler o terminal: um
        # processo num grupo de segundo plano recebe SIGTTIN e fica parado
        # (não falha, só trava) ao tentar ler `/dev/tty` — foi exatamente
        # isso que bloqueava `connector run` do GitHub até o timeout do
        # dispatcher. Capacidades com `requires_secret` (que chamam
        # `vault.with_secret()`, e podem pedir a senha mestra via
        # `/dev/tty` dentro do próprio executor — PRD-005, seção 5.2) por
        # isso herdam o grupo de processos do próprio Bamo/terminal.
        popen_kwargs["process_group"] = 0
    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except OSError as exc:
        raise ExecutorMissingError(f"não foi possível iniciar o executor: {exc}") from exc

    timeout_seconds = cap_def.get("timeout_seconds", EXECUTOR_TIMEOUT_SECONDS)
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        timeout_seconds = EXECUTOR_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout_seconds
    try:
        try:
            proc.stdin.write(request_bytes)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        stdout_data, exceeded, timed_out = _read_capped(proc, MAX_OUTPUT_BYTES, deadline)
    finally:
        _terminate(proc, own_process_group=not requires_secret)

    if timed_out:
        raise ExecutorTimeoutError(f"executor excedeu o prazo de {timeout_seconds}s")
    if exceeded:
        raise ExecutorOutputTooLargeError(f"saída do executor excede o limite de {MAX_OUTPUT_BYTES} bytes")

    try:
        response = json.loads(stdout_data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ExecutorProtocolError("saída do executor não é JSON válido") from exc

    return _validate_response(response)
