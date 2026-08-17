#!/usr/bin/env python3
"""Executor isolado do provider 'local-demo' (PRD-004, seção 5.2).

Processo filho dedicado, iniciado pelo dispatcher via subprocess sem shell
(`sys.executable -I local_demo.py`). Lê um único pedido JSON de stdin,
escreve uma única linha JSON de resposta em stdout. Não faz rede, não
importa nada do resto do Bamo (chat, memória, cofre), não lê arquivo algum
e só manipula dados sintéticos gerados aqui mesmo — prova o fluxo
conector -> dispatcher -> executor -> auditoria sem qualquer segredo ou
acesso externo real (PRD-004, seção 4, "Incluído").

Contrato de resposta (schema pequeno validado pelo dispatcher):
    {"ok": bool, "status": str, "summary": str, "safe_metadata": dict}
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

_CAPABILITIES = {"read_status", "notify_summary"}


def _synthetic_status() -> dict:
    return {"pending_items": 3, "queue": "demo-queue", "source": "local-demo"}


def _respond(ok: bool, status: str, summary: str, safe_metadata: dict) -> dict:
    return {"ok": ok, "status": status, "summary": summary, "safe_metadata": safe_metadata}


def main() -> int:
    try:
        raw = sys.stdin.read()
        request = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(json.dumps(_respond(False, "bad_request", "pedido ilegível", {})))
        return 0

    capability = request.get("capability") if isinstance(request, dict) else None
    if capability not in _CAPABILITIES:
        print(json.dumps(_respond(False, "unknown_capability", "capacidade desconhecida", {})))
        return 0

    now = datetime.now(timezone.utc).isoformat()
    if capability == "read_status":
        data = _synthetic_status()
        response = _respond(
            True,
            "ok",
            f"Status sintético lido às {now}: {data['pending_items']} itens pendentes na {data['queue']}.",
            data,
        )
    else:  # notify_summary
        response = _respond(
            True,
            "ok",
            f"Resumo sintético gerado às {now} (sem envio externo, sem rede).",
            {"channel": "local"},
        )

    print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
