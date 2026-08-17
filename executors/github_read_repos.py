#!/usr/bin/env python3
"""Executor isolado do provider 'github' (PRD-005, seções 5.2 e 5.3).

Processo filho dedicado, iniciado pelo dispatcher via subprocess sem shell
(`sys.executable -I github_read_repos.py`). Lê um único pedido JSON de
stdin (`connector_id`, `capability`, `params`, `secret_id`), escreve uma
única linha JSON de resposta em stdout. `secret_id` é só um identificador
opaco — nunca o valor da credencial, que nunca atravessa argv, variável de
ambiente, arquivo temporário ou stdin (PRD-005, seção 5.2).

Só este processo desbloqueia o cofre e só ele constrói o header de
autenticação, dentro do callback passado a `vault.with_secret` — o token
nunca é retornado, logado nem incluído em mensagem de erro. Host, caminho,
query e método HTTP são constantes de código (seção 5.3): nenhum parâmetro
do pedido influencia a URL final ou os headers enviados. Redirecionamentos
não são seguidos; TLS usa a validação padrão do sistema.

Contrato de resposta (schema pequeno validado pelo dispatcher):
    {"ok": bool, "status": str, "summary": str, "safe_metadata": dict}

Mensagens de erro vêm de uma allowlist fixa (seção 8.6) — nunca `str(exc)`
de cliente HTTP, cofre ou parser.
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

# `-I` (modo isolado) não inclui o diretório do próprio script nem o
# projeto em sys.path (verificado neste ambiente) — precisamos deste
# executor para importar `core.vault`/`core.redact`, então adicionamos a
# raiz do projeto explicitamente, calculada a partir de `__file__` (nunca
# de argv/env, que poderiam ser adulterados).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import vault  # noqa: E402
from core.redact import redact  # noqa: E402

_HOST = "api.github.com"
_PATH = "/user/repos"
_QUERY = "affiliation=owner&sort=updated&direction=desc&per_page=20"
_URL = f"https://{_HOST}{_PATH}?{_QUERY}"
_METHOD = "GET"
_TIMEOUT_SECONDS = 15
_MAX_BODY_BYTES = 256 * 1024
_MAX_ITEMS = 20
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_USER_AGENT = "bamo-agent"
_CAPABILITIES = {"read_repositories"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Recusa qualquer redirecionamento (seção 5.3): retornar None faz o
    urllib levantar HTTPError com o código 3xx original em vez de seguir
    para uma URL fora da allowlist."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N803 (assinatura da stdlib)
        return None


def _respond(ok: bool, status: str, summary: str, safe_metadata: dict) -> dict:
    redacted_summary, _ = redact(summary)
    return {"ok": ok, "status": status, "summary": redacted_summary, "safe_metadata": safe_metadata}


def _read_capped(resp, limit: int) -> bytes:
    chunks = bytearray()
    while True:
        chunk = resp.read(8192)
        if not chunk:
            break
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise ValueError("corpo excede o limite permitido")
    return bytes(chunks)


def _map_http_error(exc: urllib.error.HTTPError) -> dict:
    code = exc.code
    headers = exc.headers
    try:
        exc.close()
    except Exception:
        pass
    if code == 401:
        return _respond(False, "unauthorized", "Credencial recusada pela API do GitHub (401).", {})
    if code == 403:
        remaining = headers.get("X-RateLimit-Remaining") if headers is not None else None
        if remaining == "0":
            return _respond(False, "rate_limited", "Limite de requisições da API do GitHub atingido.", {})
        return _respond(False, "forbidden", "Acesso negado pela API do GitHub (403) — verifique o escopo do token.", {})
    if code == 404:
        return _respond(False, "not_found", "Recurso não encontrado na API do GitHub (404).", {})
    return _respond(False, "remote_error", "A API do GitHub respondeu com um erro inesperado.", {})


def _call_github(token: str) -> dict:
    """Callback de `vault.with_secret`: recebe o token só aqui, monta o
    header de autenticação e faz a única chamada HTTPS permitida. Nunca
    retorna o token nem o inclui em qualquer mensagem — só um resultado já
    seguro para atravessar de volta ao dispatcher/auditoria."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": _ACCEPT,
        "X-GitHub-Api-Version": _API_VERSION,
        "User-Agent": _USER_AGENT,
    }
    request = urllib.request.Request(_URL, method=_METHOD, headers=headers)
    opener = urllib.request.build_opener(
        _NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )

    try:
        resp = opener.open(request, timeout=_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        return _map_http_error(exc)
    except ssl.SSLError:
        return _respond(False, "remote_error", "Falha de TLS ao contatar a API do GitHub.", {})
    except (urllib.error.URLError, TimeoutError, OSError):
        return _respond(False, "remote_error", "Falha de rede ao contatar a API do GitHub.", {})

    try:
        final_url = resp.geturl()
        if not isinstance(final_url, str) or not final_url.startswith(f"https://{_HOST}/"):
            return _respond(False, "remote_error", "Resposta veio de um host fora da allowlist.", {})

        content_type = resp.headers.get_content_type() if resp.headers is not None else ""
        if content_type != "application/json":
            return _respond(False, "remote_error", "Tipo de conteúdo inesperado na resposta da API.", {})

        try:
            body = _read_capped(resp, _MAX_BODY_BYTES)
        except ValueError:
            return _respond(False, "remote_error", "Resposta da API excedeu o tamanho máximo permitido.", {})
    finally:
        resp.close()

    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _respond(False, "remote_error", "Resposta da API não é JSON válido.", {})

    if not isinstance(data, list):
        return _respond(False, "remote_error", "Resposta da API fora do schema esperado.", {})

    repos: list[dict] = []
    for item in data[:_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        full_name = item.get("full_name")
        if not isinstance(full_name, str) or not full_name:
            continue
        repos.append(
            {
                "full_name": full_name,
                "private": bool(item.get("private")),
                "updated_at": item.get("updated_at") if isinstance(item.get("updated_at"), str) else None,
            }
        )

    count = len(repos)
    if count:
        names = ", ".join(r["full_name"] for r in repos)
        summary = f"{count} repositório(s) pessoal(is) via GitHub API (GET /user/repos): {names}."
    else:
        summary = "Nenhum repositório pessoal encontrado para este token."
    return _respond(True, "ok", summary, {"count": count, "host": _HOST})


def main() -> int:
    try:
        raw = sys.stdin.read()
        request = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print(json.dumps(_respond(False, "bad_request", "Pedido ilegível.", {})))
        return 0

    capability = request.get("capability") if isinstance(request, dict) else None
    secret_id = request.get("secret_id") if isinstance(request, dict) else None

    if capability not in _CAPABILITIES:
        print(json.dumps(_respond(False, "unknown_capability", "Capacidade desconhecida.", {})))
        return 0
    if not isinstance(secret_id, str) or not secret_id:
        print(json.dumps(_respond(False, "secret_unavailable", "Credencial não configurada para este conector.", {})))
        return 0

    try:
        response = vault.with_secret(secret_id, _call_github)
    except vault.VaultError:
        response = _respond(False, "secret_unavailable", "Credencial indisponível no cofre.", {})
    except Exception:
        response = _respond(False, "internal_error", "Falha inesperada ao processar a capacidade.", {})

    print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
