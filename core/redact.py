"""Detecção e redação best-effort de segredos antes de qualquer persistência.

Cofre de segredos de verdade fica para um PRD posterior (seção 5.1 do PRD-002).
Isto aqui é uma rede de segurança local: substitui o valor reconhecido por
`[REDACTED]` antes de qualquer gravação em sessão, memória longa ou OKF.
"""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-_.=]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(
        r"(?i)(api[_-]?key|secret|token|senha|password|passwd|credencial)"
        r"\s*[:=]\s*['\"]?([^\s'\"]{6,})['\"]?"
    ),
]

_MASK = "[REDACTED]"


def redact(text: str) -> tuple[str, bool]:
    """Retorna (texto_redigido, houve_redacao)."""
    if not text:
        return text, False

    redacted = text
    found = False

    for pattern in _PATTERNS[:-1]:
        new_text, count = pattern.subn(_MASK, redacted)
        if count:
            found = True
            redacted = new_text

    def _mask_kv(match: re.Match[str]) -> str:
        return f"{match.group(1)}={_MASK}"

    new_text, count = _PATTERNS[-1].subn(_mask_kv, redacted)
    if count:
        found = True
        redacted = new_text

    return redacted, found
