"""Cofre local de segredos (PRD-003) — armazenamento criptografado, em
repouso, para credenciais informadas explicitamente pelo usuário.

Isolamento deliberado: nada neste módulo é chamado por `context_builder`,
`agy_runtime`, `sessions`, `memory_store`, `learning` ou qualquer fluxo de
chat/ask. O cofre não sabe que o resto do Bamo existe, e o resto do Bamo não
lê valores do cofre (PRD-003, seção 4.3) — essa separação é a própria defesa
contra o segredo vazar para o runtime ou para a persistência do PRD-002.

Formato em disco (`secrets/vault.enc`, um único JSON):

    envelope = {
        "file_format": 1,
        "vault_id": "vault-...",
        "created_at": <iso>,
        "key_provider": "system_keyring" | "password",
        "key_version": <int>,
        "kdf": {  # só em modo password
            "algorithm": "argon2id",
            "salt": <base64>,
            "iterations": 3, "lanes": 4, "memory_cost": 65536, "length": 32,
        } | null,
        "wrapped_dek": <base64 do token Fernet> | null,  # só em modo password
        "ciphertext": <token Fernet da carga>,
    }

A carga (payload) autenticada é um segundo JSON, cifrado com Fernet usando a
chave de dados (DEK) aleatória do cofre:

    payload = {
        "schema_version": 1, "vault_id": "vault-...", "created_at": <iso>,
        "key_version": <int>,
        "entries": [{"id", "label", "value", "created_at", "updated_at",
                      "last_used_at"}],
        "audit": [{"at", "action", "entry_id", "result"}],
    }

Rótulos e valores só existem dentro dessa carga cifrada (D-012) — o
envelope nunca contém texto secreto, só metadados de proteção da chave.

Parâmetros do Argon2id (RFC 9106, segunda opção recomendada — 64 MiB de
memória, 3 iterações, 4 lanes): medido neste ambiente em ~0.1s por
derivação, aceitável para uso interativo de CLI e acima do mínimo seguro
recomendado para hashing de senha.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from getpass import GetPassWarning, getpass
from typing import Any

from . import ids
from .atomic import ensure_dir_secure, write_bytes_atomic_secure
from .paths import SECRETS_DIR, VAULT_PATH

ARGON2_PARAMS = {"iterations": 3, "lanes": 4, "memory_cost": 65536, "length": 32}

# Limites de sanidade para o bloco `kdf` de um envelope em modo senha. Um
# `vault.enc` é dado local não confiável (pode ser adulterado, truncado ou
# vir de uma versão futura/quebrada) — estes limites impedem tanto valores
# que a lib `cryptography` rejeitaria com ValueError/OverflowError cru
# (0, negativos, tipos errados) quanto valores tecnicamente aceitos por ela
# mas absurdos para uso interativo de CLI (ex.: memory_cost na casa dos GiB,
# que travaria o processo tentando alocar memória). `length` não tem faixa:
# tem que ser exatamente 32, porque é o único tamanho de chave que o
# `Fernet(kek)` usado para desembrulhar a DEK aceita.
_KDF_INT_BOUNDS = {
    "iterations": (1, 64),
    "lanes": (1, 64),
    "memory_cost": (8, 1_048_576),  # KiB: mínimo exigido pela lib (8*lanes) .. 1 GiB
}
_KDF_REQUIRED_LENGTH = 32  # bytes brutos da KEK antes do urlsafe_b64encode — exigência do Fernet


class VaultError(Exception):
    """Base de todos os erros do cofre — nunca deve carregar valor secreto na mensagem."""


class VaultExistsError(VaultError):
    pass


class VaultNotFoundError(VaultError):
    pass


class VaultLockedError(VaultError):
    """Senha incorreta, keyring não funcional, ou DEK ausente/inacessível."""


class VaultCorruptedError(VaultError):
    pass


class DependencyMissingError(VaultError):
    pass


class VaultInputError(VaultError):
    """Entrada de segredo/senha não pôde ser lida com segurança (sem TTY, sem
    eco garantido, ou EOF) — nunca chega a pedir nem a ecoar o valor."""


InvalidLabelError = ids.InvalidIdError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_cryptography() -> Any:
    """Verifica a dependência antes de qualquer escrita (PRD-003, seção 6.7)."""
    try:
        from cryptography.fernet import Fernet, InvalidToken
        from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
    except ImportError as exc:
        raise DependencyMissingError(
            "dependência 'cryptography' ausente — instale-a antes de usar o cofre; "
            "nenhum cofre foi criado ou alterado."
        ) from exc
    return Fernet, InvalidToken, Argon2id


def _keyring_available() -> bool:
    """Best-effort: tenta importar keyring e faz um round-trip de teste. Se
    qualquer etapa falhar, trata como 'sem keyring funcional' (D-010) — nunca
    propaga a exceção, pois a ausência de keyring é um caminho esperado."""
    try:
        import keyring
    except ImportError:
        return False
    probe_service, probe_account, probe_value = "bamo-vault-probe", "probe", "probe"
    try:
        keyring.set_password(probe_service, probe_account, probe_value)
        ok = keyring.get_password(probe_service, probe_account) == probe_value
        keyring.delete_password(probe_service, probe_account)
        return ok
    except Exception:
        return False


_KEYRING_SERVICE = "bamo-vault"


def _keyring_account(vault_id: str, key_version: int) -> str:
    return f"{vault_id}:v{key_version}"


def _keyring_get_dek(vault_id: str, key_version: int) -> bytes | None:
    """Lê a DEK da versão pedida; cai para a conta antiga sem versão (cofres
    criados antes deste esquema versionado) só quando a conta versionada não
    existe."""
    try:
        import keyring
    except ImportError:
        return None
    try:
        raw = keyring.get_password(_KEYRING_SERVICE, _keyring_account(vault_id, key_version))
        if not raw:
            raw = keyring.get_password(_KEYRING_SERVICE, vault_id)
    except Exception:
        return None
    if not raw:
        return None
    return raw.encode("ascii")


def _keyring_set_dek(vault_id: str, key_version: int, dek: bytes) -> None:
    import keyring

    keyring.set_password(_KEYRING_SERVICE, _keyring_account(vault_id, key_version), dek.decode("ascii"))


def _keyring_delete_dek(vault_id: str, key_version: int) -> None:
    """Limpeza best-effort — nunca deve derrubar o chamador. Usada tanto para
    remover uma chave nova órfã após uma escrita de envelope que falhou
    quanto para remover a chave antiga depois que uma rotação já foi
    confirmada em disco. Uma falha aqui não pode mascarar o resultado real
    da chamada nem deixar o cofre inconsistente — a chave 'errada' só é
    removida depois que a correta já está confirmada em uso."""
    try:
        import keyring

        keyring.delete_password(_KEYRING_SERVICE, _keyring_account(vault_id, key_version))
    except Exception:
        pass


def _derive_kek(password: str, kdf: dict[str, Any], Argon2id: Any) -> bytes:
    salt = base64.b64decode(kdf["salt"])
    kdf_obj = Argon2id(
        salt=salt,
        length=kdf["length"],
        iterations=kdf["iterations"],
        lanes=kdf["lanes"],
        memory_cost=kdf["memory_cost"],
    )
    raw = kdf_obj.derive(password.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def _read_hidden(prompt: str) -> str:
    """Único leitor de entrada oculta do cofre — usado tanto para a senha
    mestra (`_prompt_password`) quanto para o valor de um segredo digitado
    (`prompt_secret_value`). Em vez de deixar `getpass` cair silenciosamente
    para leitura com eco quando não há TTY/`/dev/tty` disponível (o que só
    emite um aviso, sem impedir a leitura), transforma esse aviso em erro:
    ou a entrada é garantidamente oculta, ou a operação falha limpo sem
    pedir nem ecoar nada."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        try:
            value = getpass(prompt)
        except (GetPassWarning, EOFError) as exc:
            raise VaultInputError(
                "entrada oculta indisponível (sem terminal interativo com eco controlável) — "
                "rode em um terminal real, ou use --stdin quando o comando oferecer essa opção"
            ) from exc
    if not value:
        raise VaultInputError("entrada vazia")
    return value


def _prompt_password(prompt: str = "Senha mestra do cofre: ") -> str:
    return _read_hidden(prompt)


def prompt_secret_value(label: str, use_stdin: bool) -> str:
    """Lê o valor de um segredo para `secret set`. Em `--stdin`, lê o stream
    inteiro sem eco (modo automação); senão, usa o mesmo leitor oculto e
    seguro da senha mestra. Nunca imprime nem loga o valor."""
    if use_stdin:
        value = sys.stdin.read().rstrip("\n")
        if not value:
            raise VaultInputError("valor vazio — nada foi gravado")
        return value
    return _read_hidden(f"Valor de '{label}' (não será exibido): ")


def vault_exists() -> bool:
    return VAULT_PATH.exists()


_ENVELOPE_REQUIRED_STR_FIELDS = ("vault_id", "created_at", "key_provider", "ciphertext")
_ENVELOPE_KDF_FIELDS = ("algorithm", "salt", "iterations", "lanes", "memory_cost", "length")


def _validate_kdf_params(kdf: dict[str, Any]) -> None:
    """Valida por completo o bloco `kdf` de um envelope em modo senha —
    chamada por `_validate_envelope()`, então roda antes de qualquer campo
    tocar `_derive_kek()`/Argon2id/Fernet. Cobre os quatro jeitos de um
    `kdf` malformado escapar como exceção crua adiante: `algorithm`
    desconhecido, `salt` que não é Base64 válido (ou decodifica vazio),
    inteiros que não são inteiros reais (bool, string, float) e inteiros
    fora de faixas seguras (zero, negativos, ou absurdamente grandes — a
    lib `cryptography` reage a esses casos ora com `ValueError`, ora com
    `OverflowError`, então validar aqui evita depender de qual delas)."""
    if kdf.get("algorithm") != "argon2id":
        raise VaultCorruptedError(f"envelope do cofre com algoritmo de kdf desconhecido: {kdf.get('algorithm')!r}")

    salt = kdf.get("salt")
    if not isinstance(salt, str) or not salt:
        raise VaultCorruptedError("envelope do cofre com salt de kdf ausente ou inválido")
    try:
        decoded_salt = base64.b64decode(salt, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VaultCorruptedError("envelope do cofre com salt de kdf não é Base64 válido") from exc
    if not decoded_salt:
        raise VaultCorruptedError("envelope do cofre com salt de kdf vazio")

    for field, (lo, hi) in _KDF_INT_BOUNDS.items():
        value = kdf.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            raise VaultCorruptedError(f"envelope do cofre com kdf.{field} de tipo inválido")
        if not (lo <= value <= hi):
            raise VaultCorruptedError(f"envelope do cofre com kdf.{field} fora da faixa permitida ({lo}-{hi})")

    length = kdf.get("length")
    if not isinstance(length, int) or isinstance(length, bool) or length != _KDF_REQUIRED_LENGTH:
        raise VaultCorruptedError(f"envelope do cofre com kdf.length inválido (precisa ser {_KDF_REQUIRED_LENGTH})")

    # A lib `cryptography` exige memory_cost >= 8 * lanes para Argon2id; sem
    # essa checagem aqui, uma combinação dentro das faixas individuais acima
    # ainda poderia disparar ValueError direto da construção do KDF.
    if kdf["memory_cost"] < 8 * kdf["lanes"]:
        raise VaultCorruptedError("envelope do cofre com kdf.memory_cost insuficiente para kdf.lanes")


def _validate_envelope(data: Any) -> dict[str, Any]:
    """Único ponto que decide se um `vault.enc` decodificado tem o formato
    esperado — chamado por `_load_envelope()`, então protege igualmente
    `status()`, `_unlock()` e, por extensão, `rotate_key()`. Um `vault.enc`
    com JSON sintaticamente válido mas estruturalmente errado (lista,
    string, número, dict incompleto, tipos trocados) sempre vira
    VaultCorruptedError aqui — nunca escapa como KeyError/TypeError/
    AttributeError cru para quem chamou (a CLI, entre outros)."""
    if not isinstance(data, dict):
        raise VaultCorruptedError(f"envelope do cofre não é um objeto JSON (recebido {type(data).__name__})")

    for field in _ENVELOPE_REQUIRED_STR_FIELDS:
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise VaultCorruptedError(f"envelope do cofre com campo ausente ou inválido: {field!r}")

    if not isinstance(data.get("key_version"), int) or isinstance(data.get("key_version"), bool) or data["key_version"] < 1:
        raise VaultCorruptedError("envelope do cofre com key_version ausente ou inválida")

    if data["key_provider"] not in ("system_keyring", "password"):
        raise VaultCorruptedError(f"envelope do cofre com key_provider desconhecido: {data['key_provider']!r}")

    if data["key_provider"] == "password":
        kdf = data.get("kdf")
        if not isinstance(kdf, dict):
            raise VaultCorruptedError("envelope do cofre em modo senha sem parâmetros de kdf válidos")
        for field in _ENVELOPE_KDF_FIELDS:
            if field not in kdf:
                raise VaultCorruptedError(f"envelope do cofre com kdf incompleto: falta {field!r}")
        _validate_kdf_params(kdf)
        if not isinstance(data.get("wrapped_dek"), str) or not data["wrapped_dek"]:
            raise VaultCorruptedError("envelope do cofre em modo senha sem wrapped_dek válido")

    return data


def _load_envelope() -> dict[str, Any]:
    if not VAULT_PATH.exists():
        raise VaultNotFoundError("cofre não inicializado — rode 'bamo vault init' primeiro")
    try:
        data = json.loads(VAULT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VaultCorruptedError("arquivo do cofre corrompido ou ilegível") from exc
    return _validate_envelope(data)


def _save_envelope(envelope: dict[str, Any]) -> None:
    data = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    write_bytes_atomic_secure(VAULT_PATH, data)


def status() -> dict[str, Any] | None:
    """Lê só metadados do envelope — nunca desbloqueia a carga cifrada."""
    if not VAULT_PATH.exists():
        return None
    envelope = _load_envelope()
    # _load_envelope() já garante o formato via _validate_envelope(); o
    # except abaixo é só uma rede de segurança extra (nunca deve disparar).
    try:
        return {
            "vault_id": envelope["vault_id"],
            "key_provider": envelope["key_provider"],
            "key_version": envelope["key_version"],
            "created_at": envelope["created_at"],
        }
    except (KeyError, TypeError) as exc:
        raise VaultCorruptedError("envelope do cofre com campos ausentes ou de tipo inválido") from exc


def init_vault(key_provider: str | None) -> dict[str, Any]:
    if key_provider not in (None, "system", "password"):
        raise VaultError(f"provedor de chave inválido: {key_provider!r}")

    Fernet, _InvalidToken, Argon2id = _require_cryptography()

    if VAULT_PATH.exists():
        raise VaultExistsError(
            f"já existe um cofre em {VAULT_PATH} — este PRD não tem fluxo de recriação/recuperação"
        )

    use_keyring: bool
    if key_provider == "system":
        if not _keyring_available():
            raise DependencyMissingError(
                "--key-provider system pedido, mas nenhum keyring funcional foi encontrado "
                "neste ambiente — nada foi criado (sem fallback silencioso para senha)."
            )
        use_keyring = True
    elif key_provider == "password":
        use_keyring = False
    else:
        use_keyring = _keyring_available()

    vault_id = ids.new_id("vault")
    now = _now()
    dek = Fernet.generate_key()

    envelope: dict[str, Any] = {
        "file_format": 1,
        "vault_id": vault_id,
        "created_at": now,
        "key_provider": "system_keyring" if use_keyring else "password",
        "key_version": 1,
        "kdf": None,
        "wrapped_dek": None,
    }

    if use_keyring:
        _keyring_set_dek(vault_id, 1, dek)
    else:
        password = _prompt_password("Defina a senha mestra do cofre: ")
        confirm = _prompt_password("Confirme a senha mestra: ")
        if password != confirm:
            raise VaultError("as senhas não conferem — nenhum cofre foi criado")
        salt = os.urandom(16)
        kdf = {"algorithm": "argon2id", "salt": base64.b64encode(salt).decode("ascii"), **ARGON2_PARAMS}
        kek = _derive_kek(password, kdf, Argon2id)
        envelope["kdf"] = kdf
        envelope["wrapped_dek"] = Fernet(kek).encrypt(dek).decode("ascii")

    payload = {
        "schema_version": 1,
        "vault_id": vault_id,
        "created_at": now,
        "key_version": 1,
        "entries": [],
        "audit": [{"at": now, "action": "vault_init", "entry_id": None, "result": "ok"}],
    }
    envelope["ciphertext"] = Fernet(dek).encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")

    ensure_dir_secure(SECRETS_DIR)
    _save_envelope(envelope)
    return {"vault_id": vault_id, "key_provider": envelope["key_provider"], "created_at": now}


@dataclass
class _Unlocked:
    dek: bytes
    envelope: dict[str, Any]
    payload: dict[str, Any]
    kek: bytes | None = None  # só em modo password — reaproveitado por rotate_key


def _unlock() -> _Unlocked:
    Fernet, InvalidToken, Argon2id = _require_cryptography()
    envelope = _load_envelope()
    kek: bytes | None = None

    # _load_envelope() já validou o formato do envelope, incluindo o bloco
    # `kdf` inteiro (_validate_kdf_params); o try/except abaixo é uma rede de
    # segurança extra para que qualquer acesso de campo ou derivação aqui, se
    # algo escapar da validação centralizada, vire VaultCorruptedError em vez
    # de KeyError/TypeError/AttributeError/ValueError/OverflowError/
    # binascii.Error cru — inclui a própria construção do Argon2id/Fernet a
    # partir de `kdf`, que a lib `cryptography` pode rejeitar com qualquer
    # uma dessas exceções conforme o parâmetro inválido.
    try:
        if envelope["key_provider"] == "system_keyring":
            if not _keyring_available():
                raise VaultLockedError(
                    "este cofre usa o keyring do sistema, mas nenhum keyring funcional foi "
                    "encontrado agora — cofre permanece bloqueado, sem fallback para senha."
                )
            dek = _keyring_get_dek(envelope["vault_id"], envelope["key_version"])
            if not dek:
                raise VaultLockedError("chave de dados não encontrada no keyring — cofre bloqueado")
        elif envelope["key_provider"] == "password":
            password = _prompt_password()
            kek = _derive_kek(password, envelope["kdf"], Argon2id)
            try:
                dek = Fernet(kek).decrypt(envelope["wrapped_dek"].encode("ascii"))
            except InvalidToken as exc:
                raise VaultLockedError("senha incorreta") from exc
        else:
            raise VaultCorruptedError(f"key_provider desconhecido no envelope: {envelope['key_provider']!r}")
    except (KeyError, TypeError, AttributeError, ValueError, OverflowError, binascii.Error) as exc:
        raise VaultCorruptedError("envelope do cofre com campos ausentes ou de tipo inválido") from exc

    try:
        payload_bytes = Fernet(dek).decrypt(envelope["ciphertext"].encode("ascii"))
        payload = json.loads(payload_bytes.decode("utf-8"))
    except InvalidToken as exc:
        raise VaultCorruptedError("carga do cofre corrompida ou adulterada") from exc
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        raise VaultCorruptedError("carga do cofre ilegível após decifrar") from exc

    if not isinstance(payload, dict) or "entries" not in payload or "audit" not in payload:
        raise VaultCorruptedError("carga do cofre com formato inesperado após decifrar")

    return _Unlocked(dek=dek, envelope=envelope, payload=payload, kek=kek)


def _save_locked(unlocked: _Unlocked) -> None:
    from cryptography.fernet import Fernet

    ciphertext = Fernet(unlocked.dek).encrypt(json.dumps(unlocked.payload, ensure_ascii=False).encode("utf-8"))
    unlocked.envelope["ciphertext"] = ciphertext.decode("ascii")
    _save_envelope(unlocked.envelope)


def rotate_key() -> dict[str, Any]:
    """Confirmação do vault_id é responsabilidade do chamador (CLI), igual ao
    padrão de `--confirm` já usado em memory/session/knowledge — aqui só a
    rotação em si, assumindo que o alvo já foi confirmado.

    Transacional em modo `system_keyring`: a nova DEK é gravada num slot de
    keyring NOVO (versionado por `key_version`), sem tocar a chave antiga.
    Só depois que a nova carga foi confirmada em disco (escrita atômica do
    envelope bem-sucedida) é que a chave antiga é removida do keyring. Se
    qualquer etapa falhar antes disso — escrita no keyring, escrita do
    envelope, ou processo interrompido no meio — o envelope em disco ainda
    aponta para `key_version` antiga e a chave antiga continua intacta no
    keyring: o cofre continua totalmente recuperável com a combinação antiga
    de envelope + chave. Em modo senha a escrita já era atômica (um único
    arquivo, `os.replace`), então não há uma segunda etapa a coordenar."""
    Fernet, _InvalidToken, _Argon2id = _require_cryptography()
    unlocked = _unlock()
    vault_id = unlocked.envelope["vault_id"]
    old_version = unlocked.envelope["key_version"]
    new_version = old_version + 1
    new_dek = Fernet.generate_key()

    if unlocked.envelope["key_provider"] == "system_keyring":
        _keyring_set_dek(vault_id, new_version, new_dek)
        try:
            unlocked.dek = new_dek
            unlocked.envelope["key_version"] = new_version
            unlocked.payload["key_version"] = new_version
            unlocked.payload["audit"].append({"at": _now(), "action": "rotate_key", "entry_id": None, "result": "ok"})
            _save_locked(unlocked)
        except BaseException:
            # A escrita do envelope não foi confirmada — o envelope em disco
            # (se ainda existir/for o antigo) continua com old_version, cuja
            # chave segue intacta no keyring. Só limpa o slot novo, órfão.
            _keyring_delete_dek(vault_id, new_version)
            raise
        # Envelope novo confirmado em disco: só agora é seguro remover a
        # chave antiga. Falha aqui é best-effort e não derruba a rotação —
        # o cofre já está consistente e usável com a chave nova.
        _keyring_delete_dek(vault_id, old_version)
    else:
        # Reutiliza o KEK já derivado da senha em _unlock (senha correta acabou
        # de ser validada ali) — rotacionar a chave de dados não exige pedir a
        # senha de novo nem trocá-la. Já é atômico: um único arquivo trocado
        # de uma vez (sem estado externo intermediário como o keyring).
        unlocked.dek = new_dek
        unlocked.envelope["key_version"] = new_version
        unlocked.envelope["wrapped_dek"] = Fernet(unlocked.kek).encrypt(new_dek).decode("ascii")
        unlocked.payload["key_version"] = new_version
        unlocked.payload["audit"].append({"at": _now(), "action": "rotate_key", "entry_id": None, "result": "ok"})
        _save_locked(unlocked)

    return {"vault_id": vault_id, "key_version": new_version}


def set_secret(label: str, value: str) -> dict[str, Any]:
    ids.validate_label(label)
    if not value:
        raise VaultError("valor vazio — nada foi gravado")

    unlocked = _unlock()
    entry_id = ids.new_id("sec")
    now = _now()
    unlocked.payload["entries"].append(
        {"id": entry_id, "label": label, "value": value, "created_at": now, "updated_at": now, "last_used_at": None}
    )
    unlocked.payload["audit"].append({"at": now, "action": "secret_set", "entry_id": entry_id, "result": "ok"})
    _save_locked(unlocked)
    return {"id": entry_id, "label": label, "created_at": now}


def list_secrets() -> list[dict[str, Any]]:
    unlocked = _unlock()
    return [
        {
            "id": e["id"],
            "label": e["label"],
            "created_at": e["created_at"],
            "updated_at": e["updated_at"],
            "last_used_at": e.get("last_used_at"),
        }
        for e in unlocked.payload["entries"]
    ]


def delete_secret(entry_id: str) -> bool:
    """Confirmação de `--confirm` é responsabilidade do chamador (CLI), igual
    ao padrão já usado em memory/session/knowledge."""
    ids.validate_id(entry_id)
    unlocked = _unlock()
    entries = unlocked.payload["entries"]
    remaining = [e for e in entries if e["id"] != entry_id]
    if len(remaining) == len(entries):
        return False

    unlocked.payload["entries"] = remaining
    unlocked.payload["audit"].append({"at": _now(), "action": "secret_delete", "entry_id": entry_id, "result": "ok"})
    _save_locked(unlocked)
    return True


def audit_log() -> list[dict[str, Any]]:
    unlocked = _unlock()
    return list(unlocked.payload["audit"])


def with_secret(secret_id: str, callback: Any) -> Any:
    """API interna mínima, não exposta pela CLI (PRD-005, seção 5.2): usada
    só por um executor autorizado (`executors/*.py`) para usar uma
    credencial pelo ID. Desbloqueia o cofre no próprio processo que precisa
    da credencial, fornece o valor apenas a `callback(value)` e descarta a
    referência ao final. O retorno de `callback` deve ser um resultado
    seguro (ex.: um resumo já redigido) — nunca o valor do segredo. Qualquer
    exceção do callback vira VaultError com mensagem fixa, nunca `str(exc)`
    do chamador, para não arriscar vazar o segredo numa mensagem de erro
    montada fora do cofre."""
    ids.validate_id(secret_id)
    unlocked = _unlock()
    entry = next((e for e in unlocked.payload["entries"] if e["id"] == secret_id), None)
    if entry is None:
        raise VaultError("credencial não encontrada no cofre")

    value = entry["value"]
    try:
        result = callback(value)
    except Exception as exc:
        raise VaultError("uso da credencial falhou") from exc
    finally:
        value = None

    now = _now()
    entry["last_used_at"] = now
    unlocked.payload["audit"].append({"at": now, "action": "secret_use", "entry_id": secret_id, "result": "ok"})
    _save_locked(unlocked)
    return result
