"""Geração e validação de identificadores locais (sessão, memória, OKF).

Todo ID vindo do usuário passa por `validate_id` antes de tocar o disco,
para impedir path traversal (item 5.2.3 do PRD-002).
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

_ID_PATTERN = re.compile(r"^[a-z]+-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


class InvalidIdError(ValueError):
    """ID ausente, malformado ou com sinais de path traversal."""


def new_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(3)
    return f"{prefix}-{stamp}-{suffix}"


def validate_id(raw_id: str) -> str:
    """Garante que `raw_id` é um ID gerado pelo Bamo, nunca um caminho."""
    if not raw_id or "/" in raw_id or "\\" in raw_id or ".." in raw_id:
        raise InvalidIdError(f"ID inválido: {raw_id!r}")
    if not _ID_PATTERN.match(raw_id):
        raise InvalidIdError(f"ID inválido: {raw_id!r}")
    return raw_id


_LABEL_MAX_LEN = 200


def validate_label(label: str) -> str:
    """Garante que um rótulo de segredo (PRD-003, seção 6.2) não pode ser
    usado para formar caminho nem carregar caracteres de controle. Rótulos
    não viram nome de arquivo em nenhum ponto do cofre, mas a validação é a
    mesma exigida para qualquer entrada de usuário que acaba persistida."""
    if not label or not label.strip():
        raise InvalidIdError("rótulo vazio")
    if len(label) > _LABEL_MAX_LEN:
        raise InvalidIdError(f"rótulo excede o tamanho máximo de {_LABEL_MAX_LEN} caracteres")
    if "/" in label or "\\" in label or ".." in label:
        raise InvalidIdError(f"rótulo inválido: {label!r}")
    if any(ord(c) < 32 or ord(c) == 127 for c in label):
        raise InvalidIdError("rótulo contém caracteres de controle")
    return label
