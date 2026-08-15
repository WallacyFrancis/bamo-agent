#!/usr/bin/env python3
"""Entrada local mínima do bamo-agent usando o agy como runtime."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MEMORY = ROOT / "memory" / "CORE.md"
SKILLS = ROOT / "skills"


def system_context() -> str:
    memory = MEMORY.read_text(encoding="utf-8") if MEMORY.exists() else "(sem memória inicial)"
    skill_names = sorted(path.name for path in SKILLS.iterdir() if path.is_dir())
    return f"""Você é Bamo, um assistente local executado pelo runtime agy.

Seu workspace é: {ROOT}

Memória-base:
{memory}

Skills instaladas: {', '.join(skill_names) or '(nenhuma)'}.

Regras invioláveis deste MVP:
1. Leia e siga as skills relevantes em {SKILLS}.
2. Nunca crie, edite, mova ou remova nada dentro de {SKILLS} sem pedido explícito do usuário.
3. Não registre memórias automaticamente nesta fase. Quando algo parecer útil, proponha o conteúdo e peça confirmação.
4. Guarde pesquisa aprovada apenas em {ROOT / 'knowledge'} e memória aprovada apenas em {ROOT / 'memory'}.
5. Seja transparente sobre ações, limites e incertezas.
"""


def run_agy(arguments: list[str]) -> int:
    executable = shutil.which("agy")
    if not executable:
        print("Erro: 'agy' não foi encontrado no PATH. Instale-o ou ajuste seu PATH.", file=sys.stderr)
        return 127
    return subprocess.run([executable, *arguments], cwd=ROOT, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="bamo-agent MVP (runtime: agy)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("chat", help="abre uma sessão interativa do Bamo")
    ask = sub.add_parser("ask", help="faz uma pergunta única ao Bamo")
    ask.add_argument("prompt", help="pergunta ou tarefa")
    args = parser.parse_args()

    context = system_context()
    if args.command == "chat":
        return run_agy(["--prompt-interactive", context])
    return run_agy(["--print", f"{context}\n\nPedido do usuário:\n{args.prompt}"])


if __name__ == "__main__":
    raise SystemExit(main())
