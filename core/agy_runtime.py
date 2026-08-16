"""Única porta de entrada para o runtime `agy`.

Usa sempre `agy --print` em modo texto padrão (nunca --output-format
json/stream-json: nesta instalação, qualquer um dos dois falha com
"no output produced" por um pedido de permissão de comando que o modo
headless não consegue aprovar — ver PRD-002, seção "Contexto" do plano).
Como consequência, o agy nunca é usado para chamar ferramentas ou gravar
arquivos: só responde texto. Toda persistência é feita pelo Bamo.

Também absorve a mitigação do PRD-001 para a falha silenciosa observada no
cold-start do backend do agy: se ele sair com erro e sem nenhuma saída,
tenta de novo uma vez antes de desistir.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 3
_PRINT_TIMEOUT_SECONDS = 300


@dataclass
class AgyResult:
    ok: bool
    text: str
    error: str


def agy_available() -> bool:
    return shutil.which("agy") is not None


def call(prompt: str, *, cwd: Path) -> AgyResult:
    """Executa `agy --print <prompt>` e devolve o resultado sem imprimir nada.

    O chamador decide o que mostrar ao usuário e o que fazer em caso de falha.
    """
    executable = shutil.which("agy")
    if not executable:
        return AgyResult(ok=False, text="", error="'agy' não foi encontrado no PATH.")

    result = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            result = subprocess.run(
                [executable, "--print", prompt],
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=_PRINT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return AgyResult(ok=False, text="", error="'agy' excedeu o tempo limite de resposta.")

        produced_output = bool(result.stdout.strip() or result.stderr.strip())
        if result.returncode == 0 or produced_output:
            break
        if attempt < _ATTEMPTS:
            time.sleep(_RETRY_DELAY_SECONDS)

    assert result is not None
    if result.returncode == 0:
        return AgyResult(ok=True, text=result.stdout, error="")

    if result.stdout.strip() or result.stderr.strip():
        return AgyResult(ok=False, text=result.stdout, error=result.stderr or result.stdout)

    return AgyResult(
        ok=False,
        text="",
        error=(
            "'agy' encerrou sem produzir saída, mesmo após nova tentativa. "
            "Isso costuma indicar que o runtime ainda está inicializando; tente novamente em alguns segundos."
        ),
    )
