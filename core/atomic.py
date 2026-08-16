"""Escrita atômica compartilhada pelos stores (sessão, memória, OKF, settings)."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json_atomic(path: Path, data: Any) -> None:
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def ensure_dir_secure(path: Path, mode: int = 0o700) -> None:
    """Cria o diretório se preciso e restringe a permissão (best-effort fora de
    POSIX). Usado pelo cofre (PRD-003, seção 4.1): `secrets/` precisa ficar
    0700, não só herdar o umask do processo."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def write_bytes_atomic_secure(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Escrita atômica com fsync e permissão restrita antes da troca — para
    dados sensíveis (cofre) onde o arquivo temporário nunca pode ficar
    legível por outros processos, mesmo que a escrita falhe no meio (PRD-003,
    seção 4.1: "arquivo temporário não pode permanecer após sucesso ou
    falha").

    O nome do temporário é aleatório (não `<nome>.tmp` previsível) e a
    criação usa O_EXCL (falha se já existir algo nesse caminho) + O_NOFOLLOW
    (recusa symlink) — evita que outro processo local plante um symlink ou
    grave nesse arquivo antes da troca atômica. Só usado pelo cofre; as
    escritas atômicas comuns do PRD-002 (`write_text_atomic`/
    `write_json_atomic`) não usam esta função."""
    ensure_dir_secure(path.parent)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(tmp, flags, mode)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        if tmp.exists():
            tmp.unlink()
