#!/usr/bin/env python3
"""CLI do bamo-agent — chat/ask mediados pelo Bamo, memória de longo prazo,
sessões e conhecimento (OKF). Runtime único: agy. Ver PRD-001 e PRD-002 em
../prd-bamo-agent/prds/."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from core import (
    agy_runtime,
    connector_audit,
    connectors,
    context_builder,
    dispatcher,
    ids,
    knowledge_store,
    learning,
    memory_store,
    ranking,
    scheduler,
    sessions,
    vault,
)
from core.ids import InvalidIdError
from core.paths import ROOT
from core.redact import redact

EXIT_WORDS = {"sair", "/sair", "exit", "quit"}


def _agy_missing() -> int:
    print("Erro: 'agy' não foi encontrado no PATH. Instale-o ou ajuste seu PATH.", file=sys.stderr)
    return 127


def _run_turn(session: dict, user_text: str) -> tuple[bool, str]:
    # Redige a entrada ANTES de montar contexto ou chamar o agy: um segredo
    # nunca deve sair da máquina do usuário para o prompt do runtime, e não
    # só ficar de fora da persistência (PRD-002, seção 5.1).
    redacted_user, _ = redact(user_text)

    built = context_builder.build(session, redacted_user)
    result = agy_runtime.call(built.text, cwd=ROOT)

    sessions.append_turn(
        session,
        "user",
        redacted_user,
        retrieved_memory_ids=built.memory_ids,
        retrieved_okf_ids=[built.okf_id] if built.okf_id else None,
    )

    if not result.ok:
        sessions.append_turn(session, "bamo", result.error, error=True)
        return False, result.error

    reply = result.text.strip()
    sessions.append_turn(session, "bamo", reply)
    return True, reply


def cmd_ask(prompt: str) -> int:
    if not agy_runtime.agy_available():
        return _agy_missing()

    sessions.cleanup_expired()
    session = sessions.create(ROOT)
    ok, text = _run_turn(session, prompt)
    if not ok:
        print(f"Erro: {text}", file=sys.stderr)
    else:
        print(text)
    learning.run_cycle(session, final=True)
    sessions.end(session)
    return 0 if ok else 1


def cmd_chat() -> int:
    if not agy_runtime.agy_available():
        return _agy_missing()

    sessions.cleanup_expired()
    session = sessions.create(ROOT)
    print(f"Bamo pronto (sessão {session['id']}). Digite sua mensagem ou 'sair' para encerrar.")

    try:
        while True:
            try:
                user_text = input("Você: ")
            except EOFError:
                print()
                break
            if user_text.strip().lower() in EXIT_WORDS:
                break
            if not user_text.strip():
                continue

            ok, text = _run_turn(session, user_text)
            if ok:
                print(f"Bamo: {text}")
            else:
                print(f"Bamo: [erro: {text}]")
                continue

            learning.run_cycle(session, final=False)
    except KeyboardInterrupt:
        print()
    finally:
        learning.run_cycle(session, final=True)
        sessions.end(session)
        print(f"[sessão {session['id']} encerrada]")

    return 0


def cmd_memory_list(status: str | None) -> int:
    memories = memory_store.list_all(status=status)
    if not memories:
        print("(nenhuma memória encontrada)")
        return 0
    for m in memories:
        print(f"[{m['id']}] ({m['estado']}, {m['tipo']}, imp={m['importancia']}, conf={m['confianca']:.2f}) {m['conteudo'][:80]}")
    return 0


def cmd_memory_show(memory_id: str) -> int:
    memory = memory_store.load(memory_id)
    if not memory:
        print(f"Memória não encontrada: {memory_id}", file=sys.stderr)
        return 1
    print(json.dumps(memory, ensure_ascii=False, indent=2))
    origin_session = memory.get("origem", {}).get("session_id")
    if origin_session and not sessions.exists(origin_session):
        print("(sessão de origem expirada — transcript removido; a memória continua válida)")
    return 0


def cmd_memory_search(query: str) -> int:
    ranked = ranking.rank(memory_store.list_active(), query, limit=10)
    if not ranked:
        print("(nenhuma memória relevante)")
        return 0
    for m in ranked:
        print(f"[{m['id']}] {m['conteudo'][:80]}")
    return 0


def cmd_memory_correct(memory_id: str, content: str | None) -> int:
    old = memory_store.load(memory_id)
    if not old:
        print(f"Memória não encontrada: {memory_id}", file=sys.stderr)
        return 1
    if content is None:
        print("Novo conteúdo (finalize com Ctrl+D):")
        content = sys.stdin.read().strip()
    if not content.strip():
        print("Conteúdo vazio — nada foi corrigido.", file=sys.stderr)
        return 2

    try:
        validated = memory_store.validate_candidate(
            {
                "tipo": old["tipo"],
                "conteudo": content,
                "tags": old["tags"],
                "importancia": old["importancia"],
                "confianca": 1.0,
                "justificativa": "correção manual do usuário",
            }
        )
    except memory_store.InvalidMemoryError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2

    # Sem redact() explícito aqui: memory_store.create()/save() já redige
    # qualquer segredo antes de gravar (proteção centralizada no store).
    origin = {"session_id": None, "turn_ids": [], "tipo": "user_correction"}
    new_memory = memory_store.create(validated, origem=origin, supersedes=old["id"])
    print(f"Memória corrigida: {old['id']} (superseded) -> {new_memory['id']}")
    return 0


def cmd_memory_forget(memory_id: str, confirm: str | None) -> int:
    if confirm != memory_id:
        print(f"Alvo: {memory_id}")
        print(f"Para confirmar a exclusão, rode: bamo memory forget {memory_id} --confirm {memory_id}")
        return 2
    if memory_store.forget(memory_id):
        print(f"Memória removida: {memory_id}")
        return 0
    print(f"Memória não encontrada: {memory_id}", file=sys.stderr)
    return 1


def cmd_memory_block(memory_id: str) -> int:
    memory = memory_store.set_state(memory_id, "blocked")
    if not memory:
        print(f"Memória não encontrada: {memory_id}", file=sys.stderr)
        return 1
    print(f"Memória bloqueada (fora do contexto, não apagada): {memory_id}")
    return 0


def cmd_memory_learning(mode: str) -> int:
    learning.set_learning_enabled(mode == "on")
    estado = "ativado" if mode == "on" else "desativado"
    print(f"Aprendizado automático {estado}.")
    return 0


def cmd_session_list() -> int:
    sessions.cleanup_expired()
    items = sessions.list_sessions()
    if not items:
        print("(nenhuma sessão encontrada)")
        return 0
    for s in items:
        print(f"[{s.id}] {s.status} — {s.turn_count} turnos — criada em {s.created_at}")
    return 0


def cmd_session_show(session_id: str) -> int:
    session = sessions.load(session_id)
    if not session:
        print(f"Sessão não encontrada: {session_id}", file=sys.stderr)
        return 1
    print(json.dumps(session, ensure_ascii=False, indent=2))
    return 0


def cmd_session_delete(session_id: str, confirm: str | None) -> int:
    if confirm != session_id:
        print(f"Alvo: {session_id}")
        print(f"Para confirmar a exclusão, rode: bamo session delete {session_id} --confirm {session_id}")
        return 2
    if sessions.delete(session_id):
        print(f"Sessão removida: {session_id}")
        return 0
    print(f"Sessão não encontrada: {session_id}", file=sys.stderr)
    return 1


def cmd_knowledge_list() -> int:
    items = knowledge_store.list_all()
    if not items:
        print("(nenhum OKF encontrado)")
        return 0
    for k in items:
        print(f"[{k['id']}] ({k['status']}, {k['confidence']}) {k['title']}")
    return 0


def cmd_knowledge_show(okf_id: str) -> int:
    okf = knowledge_store.load(okf_id)
    if not okf:
        print(f"OKF não encontrado: {okf_id}", file=sys.stderr)
        return 1
    print(json.dumps(okf, ensure_ascii=False, indent=2))
    return 0


def cmd_knowledge_forget(okf_id: str, confirm: str | None) -> int:
    if confirm != okf_id:
        print(f"Alvo: {okf_id}")
        print(f"Para confirmar a exclusão, rode: bamo knowledge forget {okf_id} --confirm {okf_id}")
        return 2
    if knowledge_store.forget(okf_id):
        print(f"OKF removido: {okf_id}")
        return 0
    print(f"OKF não encontrado: {okf_id}", file=sys.stderr)
    return 1


def cmd_vault_init(key_provider: str | None) -> int:
    try:
        info = vault.init_vault(key_provider)
    except vault.VaultError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    print(f"Cofre criado: {info['vault_id']} (provedor de chave: {info['key_provider']})")
    return 0


def cmd_vault_status() -> int:
    try:
        info = vault.status()
    except vault.VaultError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    if not info:
        print("(cofre não inicializado — rode 'bamo vault init')")
        return 0
    print(
        f"vault_id={info['vault_id']} provedor={info['key_provider']} "
        f"key_version={info['key_version']} criado_em={info['created_at']}"
    )
    return 0


def cmd_vault_lock() -> int:
    print(
        "O cofre do Bamo pede a senha/keyring a cada comando — não há sessão "
        "destravada entre comandos para travar agora."
    )
    return 0


def cmd_vault_rotate_key(confirm: str | None) -> int:
    try:
        info = vault.status()
    except vault.VaultError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    if not info:
        print("Erro: cofre não inicializado.", file=sys.stderr)
        return 1
    if confirm != info["vault_id"]:
        print(f"Alvo: {info['vault_id']}")
        print(f"Para confirmar a rotação, rode: bamo vault rotate-key --confirm {info['vault_id']}")
        return 2
    try:
        result = vault.rotate_key()
    except vault.VaultError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    print(f"Chave de dados rotacionada: {result['vault_id']} (key_version={result['key_version']})")
    return 0


def cmd_secret_set(label: str, use_stdin: bool) -> int:
    # Verifica que o cofre existe e está acessível ANTES de pedir/ler o valor
    # — evita que o usuário digite um segredo para uma operação que já ia
    # falhar de qualquer jeito (cofre ausente ou envelope corrompido).
    try:
        if vault.status() is None:
            print("Erro: cofre não inicializado — rode 'bamo vault init' primeiro.", file=sys.stderr)
            return 1
    except vault.VaultError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    try:
        value = vault.prompt_secret_value(label, use_stdin)
    except vault.VaultError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1

    try:
        info = vault.set_secret(label, value)
    except vault.VaultError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    print(f"Segredo gravado: {info['id']} ({info['label']})")
    return 0


def cmd_secret_list() -> int:
    try:
        entries = vault.list_secrets()
    except vault.VaultError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    if not entries:
        print("(nenhum segredo armazenado)")
        return 0
    for e in entries:
        last_used = e["last_used_at"] or "nunca"
        print(f"[{e['id']}] {e['label']} — criado em {e['created_at']}, último uso: {last_used}")
    return 0


def cmd_secret_delete(entry_id: str, confirm: str | None) -> int:
    if confirm != entry_id:
        print(f"Alvo: {entry_id}")
        print(f"Para confirmar a exclusão, rode: bamo secret delete {entry_id} --confirm {entry_id}")
        return 2
    try:
        found = vault.delete_secret(entry_id)
    except vault.VaultError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    if found:
        print(f"Segredo removido: {entry_id}")
        return 0
    print(f"Segredo não encontrado: {entry_id}", file=sys.stderr)
    return 1


def cmd_secret_audit() -> int:
    try:
        entries = vault.audit_log()
    except vault.VaultError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    if not entries:
        print("(nenhum evento de auditoria)")
        return 0
    for a in entries:
        entry_ref = a.get("entry_id") or "-"
        print(f"{a['at']} {a['action']} entry={entry_ref} resultado={a['result']}")
    return 0


def cmd_connector_list() -> int:
    try:
        items = connectors.list_all()
    except connectors.ConnectorError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    if not items:
        print("(nenhum conector encontrado)")
        return 0
    for c in items:
        estado = "habilitado" if c["enabled"] else "desabilitado"
        print(f"[{c['id']}] {c['display_name']} (provider={c['provider']}, {estado}) — capacidades: {', '.join(c['capabilities'])}")
    return 0


def cmd_connector_show(connector_id: str) -> int:
    try:
        connector = connectors.load(connector_id)
    except connectors.ConnectorError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    if connector is None:
        print(f"Conector não encontrado: {connector_id}", file=sys.stderr)
        return 1
    print(json.dumps(connector, ensure_ascii=False, indent=2))
    return 0


def cmd_connector_create_demo(name: str) -> int:
    try:
        record = connectors.create_demo(name)
    except connectors.ConnectorError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    print(f"Conector criado: {record['id']} ({record['display_name']}, provider={record['provider']})")
    return 0


def cmd_connector_create(provider_name: str, secret_id: str | None, confirm: str | None) -> int:
    provider = connectors.PROVIDERS.get(provider_name)
    if provider is None:
        print(f"Erro: provider desconhecido: {provider_name}", file=sys.stderr)
        return 2
    if not provider.get("requires_secret_id"):
        print(
            f"Erro: provider {provider_name!r} não usa credencial do cofre "
            "— use 'bamo connector create-demo' para providers de demonstração.",
            file=sys.stderr,
        )
        return 2
    if not secret_id:
        print("Erro: este provider exige --secret-id <sec-id>.", file=sys.stderr)
        return 2
    ids.validate_id(secret_id)

    try:
        vault_secret_ids = {entry["id"] for entry in vault.list_secrets()}
    except vault.VaultError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    if secret_id not in vault_secret_ids:
        print(f"Erro: secret_id não encontrado no cofre: {secret_id}", file=sys.stderr)
        return 1

    if confirm != secret_id:
        print(f"Provider: {provider_name} ({provider['display_name']})")
        print(f"Host autorizado: {', '.join(provider['allowed_origins'])}")
        print(f"Capacidades: {', '.join(sorted(provider['capabilities'].keys()))}")
        print(f"Credencial (secret_id): {secret_id}")
        print(
            f"Para confirmar, rode: bamo connector create {provider_name} "
            f"--secret-id {secret_id} --confirm {secret_id}"
        )
        return 2

    try:
        record = connectors.create(provider_name, secret_id)
    except connectors.ConnectorError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    print(f"Conector criado: {record['id']} ({record['display_name']}, provider={record['provider']})")
    return 0


def cmd_connector_enable(connector_id: str, confirm: str | None) -> int:
    if confirm != connector_id:
        print(f"Alvo: {connector_id}")
        print(f"Para confirmar, rode: bamo connector enable {connector_id} --confirm {connector_id}")
        return 2
    try:
        connectors.enable(connector_id)
    except connectors.ConnectorError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    print(f"Conector habilitado: {connector_id}")
    return 0


def cmd_connector_disable(connector_id: str, confirm: str | None) -> int:
    if confirm != connector_id:
        print(f"Alvo: {connector_id}")
        print(f"Para confirmar, rode: bamo connector disable {connector_id} --confirm {connector_id}")
        return 2
    try:
        connectors.disable(connector_id)
    except connectors.ConnectorError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    # PRD-005, seção 9: desabilitar cancela agendas associadas, igual à
    # exclusão — evita deixar uma agenda "habilitada" apontando para um
    # conector que não pode mais executar nada até ser reabilitado.
    removed_schedules = scheduler.delete_for_connector(connector_id)
    print(f"Conector desabilitado: {connector_id} ({removed_schedules} agenda(s) associada(s) removida(s))")
    return 0


def cmd_connector_delete(connector_id: str, confirm: str | None) -> int:
    if confirm != connector_id:
        print(f"Alvo: {connector_id}")
        print(f"Para confirmar a exclusão, rode: bamo connector delete {connector_id} --confirm {connector_id}")
        return 2
    try:
        found = connectors.delete(connector_id)
    except connectors.ConnectorError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    if not found:
        print(f"Conector não encontrado: {connector_id}", file=sys.stderr)
        return 1
    removed_schedules = scheduler.delete_for_connector(connector_id)
    print(f"Conector removido: {connector_id} ({removed_schedules} agenda(s) associada(s) removida(s))")
    return 0


def cmd_connector_run(connector_id: str, capability: str, confirm: str | None) -> int:
    try:
        connector = connectors.load(connector_id)
    except connectors.ConnectorError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    if connector is None:
        print(f"Conector não encontrado: {connector_id}", file=sys.stderr)
        return 1

    provider = connectors.PROVIDERS.get(connector["provider"])
    cap_def = provider["capabilities"].get(capability) if provider else None
    if cap_def is None or capability not in connector["capabilities"]:
        print(f"Erro: capacidade não habilitada para este conector: {capability}", file=sys.stderr)
        return 2

    if confirm != connector_id:
        print(f"Conector: {connector_id} ({connector['display_name']}, provider={connector['provider']})")
        print(f"Capacidade: {capability} ({cap_def['kind']}) — {cap_def['description']}")
        print(f"Para confirmar a execução, rode: bamo connector run {connector_id} {capability} --confirm {connector_id}")
        return 2

    if not connector["enabled"]:
        print("Erro: conector desabilitado.", file=sys.stderr)
        return 1

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        outcome = dispatcher.run_capability(connector, capability)
        ok, status, summary = outcome["ok"], outcome["status"], outcome["summary"]
    except dispatcher.DispatcherError as exc:
        ok, status, summary = False, exc.__class__.__name__, str(exc)
    ended_at = datetime.now(timezone.utc).isoformat()

    connector_audit.append_event(
        connector_id=connector_id,
        capability=capability,
        origin="manual",
        started_at=started_at,
        ended_at=ended_at,
        ok=ok,
        status=status,
        summary=summary,
    )

    if not ok:
        print(f"Erro: execução falhou ({status}): {summary}", file=sys.stderr)
        return 1
    print(f"OK ({status}): {summary}")
    return 0


def cmd_connector_audit(connector_id: str | None) -> int:
    connector_audit.cleanup_expired()
    events = connector_audit.list_events(connector_id)
    if not events:
        print("(nenhum evento de auditoria)")
        return 0
    for e in events:
        print(f"{e['at']} conector={e['connector_id']} capacidade={e['capability']} origem={e['origin']} ok={e['ok']} status={e['status']}")
    return 0


def cmd_schedule_set(connector_id: str, capability: str, every_minutes: int, confirm: str | None) -> int:
    if confirm != connector_id:
        print(f"Alvo: {connector_id}")
        print(
            f"Para confirmar, rode: bamo schedule set {connector_id} {capability} "
            f"--every {every_minutes} --confirm {connector_id}"
        )
        return 2
    try:
        record = scheduler.set_schedule(connector_id, capability, every_minutes)
    except scheduler.SchedulerError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    print(f"Agenda criada: {record['id']} ({connector_id}/{capability} a cada {every_minutes} min)")
    return 0


def cmd_schedule_list() -> int:
    try:
        items = scheduler.list_all()
    except scheduler.SchedulerError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    if not items:
        print("(nenhuma agenda encontrada)")
        return 0
    for s in items:
        estado = "habilitada" if s["enabled"] else "desabilitada"
        last = s["last_run_at"] or "nunca"
        print(f"[{s['id']}] {s['connector_id']}/{s['capability']} a cada {s['every_minutes']}min ({estado}) — última execução: {last}")
    return 0


def cmd_schedule_disable(schedule_id: str, confirm: str | None) -> int:
    if confirm != schedule_id:
        print(f"Alvo: {schedule_id}")
        print(f"Para confirmar, rode: bamo schedule disable {schedule_id} --confirm {schedule_id}")
        return 2
    try:
        found = scheduler.disable(schedule_id)
    except scheduler.SchedulerError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    if found:
        print(f"Agenda desabilitada: {schedule_id}")
        return 0
    print(f"Agenda não encontrada: {schedule_id}", file=sys.stderr)
    return 1


def cmd_scheduler_run() -> int:
    connector_audit.cleanup_expired()
    try:
        results = scheduler.run_due()
    except scheduler.SchedulerBusyError as exc:
        print(f"Aviso: {exc}", file=sys.stderr)
        return 0
    except scheduler.SchedulerError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    if not results:
        print("(nenhuma agenda devida agora)")
        return 0
    for r in results:
        print(f"[{r['schedule_id']}] {r['status']} (ok={r['ok']})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="bamo-agent (runtime: agy)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("chat", help="abre uma sessão de conversa mediada pelo Bamo")

    ask_p = sub.add_parser("ask", help="faz uma pergunta única ao Bamo")
    ask_p.add_argument("prompt", help="pergunta ou tarefa")

    memory_p = sub.add_parser("memory", help="gerencia memória de longo prazo")
    memory_sub = memory_p.add_subparsers(dest="memory_command", required=True)

    m_list = memory_sub.add_parser("list", help="lista memórias")
    m_list.add_argument("--status", choices=sorted(memory_store.VALID_STATES), default=None)

    m_show = memory_sub.add_parser("show", help="mostra uma memória")
    m_show.add_argument("id")

    m_search = memory_sub.add_parser("search", help="busca memórias relevantes")
    m_search.add_argument("query")

    m_correct = memory_sub.add_parser("correct", help="corrige uma memória (marca a antiga como superseded)")
    m_correct.add_argument("id")
    m_correct.add_argument("--content", default=None, help="novo conteúdo; sem isso, lê do stdin")

    m_forget = memory_sub.add_parser("forget", help="apaga uma memória (exige --confirm)")
    m_forget.add_argument("id")
    m_forget.add_argument("--confirm", default=None)

    m_block = memory_sub.add_parser("block", help="bloqueia uma memória (some do contexto, não é apagada)")
    m_block.add_argument("id")

    m_learning = memory_sub.add_parser("learning", help="liga/desliga o aprendizado automático")
    m_learning.add_argument("mode", choices=["on", "off"])

    session_p = sub.add_parser("session", help="gerencia sessões de conversa")
    session_sub = session_p.add_subparsers(dest="session_command", required=True)

    session_sub.add_parser("list", help="lista sessões")

    s_show = session_sub.add_parser("show", help="mostra uma sessão")
    s_show.add_argument("id")

    s_delete = session_sub.add_parser("delete", help="apaga uma sessão (exige --confirm)")
    s_delete.add_argument("id")
    s_delete.add_argument("--confirm", default=None)

    knowledge_p = sub.add_parser("knowledge", help="gerencia OKFs (conhecimento consolidado)")
    knowledge_sub = knowledge_p.add_subparsers(dest="knowledge_command", required=True)

    knowledge_sub.add_parser("list", help="lista OKFs")

    k_show = knowledge_sub.add_parser("show", help="mostra um OKF")
    k_show.add_argument("id")

    k_forget = knowledge_sub.add_parser("forget", help="apaga um OKF (exige --confirm)")
    k_forget.add_argument("id")
    k_forget.add_argument("--confirm", default=None)

    vault_p = sub.add_parser("vault", help="gerencia o cofre local de segredos")
    vault_sub = vault_p.add_subparsers(dest="vault_command", required=True)

    v_init = vault_sub.add_parser("init", help="inicializa o cofre")
    v_init.add_argument("--key-provider", choices=["system", "password"], default=None)

    vault_sub.add_parser("status", help="mostra metadados do cofre (sem desbloquear)")
    vault_sub.add_parser("lock", help="informa que não há sessão destravada a travar")

    v_rotate = vault_sub.add_parser("rotate-key", help="rotaciona a chave de dados (exige --confirm)")
    v_rotate.add_argument("--confirm", default=None)

    secret_p = sub.add_parser("secret", help="gerencia segredos dentro do cofre")
    secret_sub = secret_p.add_subparsers(dest="secret_command", required=True)

    s_set = secret_sub.add_parser("set", help="grava um segredo (entrada oculta por padrão)")
    s_set.add_argument("label")
    s_set.add_argument("--stdin", action="store_true", help="lê o valor do stdin em vez de pedir na tela")

    secret_sub.add_parser("list", help="lista segredos (sem valores)")

    s_delete = secret_sub.add_parser("delete", help="apaga um segredo (exige --confirm)")
    s_delete.add_argument("id")
    s_delete.add_argument("--confirm", default=None)

    secret_sub.add_parser("audit", help="mostra o log de auditoria do cofre (sem rótulos/valores)")

    connector_p = sub.add_parser("connector", help="gerencia conectores com capacidades explícitas (PRD-004)")
    connector_sub = connector_p.add_subparsers(dest="connector_command", required=True)

    connector_sub.add_parser("list", help="lista conectores")

    c_show = connector_sub.add_parser("show", help="mostra um conector")
    c_show.add_argument("id")

    c_create = connector_sub.add_parser("create-demo", help="cria o conector de demonstração local-demo")
    c_create.add_argument("nome")

    c_create_real = connector_sub.add_parser(
        "create", help="cria um conector para um provider real com credencial do cofre (exige --secret-id e --confirm)"
    )
    c_create_real.add_argument("provider")
    c_create_real.add_argument("--secret-id", default=None, dest="secret_id")
    c_create_real.add_argument("--confirm", default=None)

    c_enable = connector_sub.add_parser("enable", help="habilita um conector (exige --confirm)")
    c_enable.add_argument("id")
    c_enable.add_argument("--confirm", default=None)

    c_disable = connector_sub.add_parser("disable", help="desabilita um conector (exige --confirm)")
    c_disable.add_argument("id")
    c_disable.add_argument("--confirm", default=None)

    c_delete = connector_sub.add_parser("delete", help="apaga um conector e suas agendas (exige --confirm)")
    c_delete.add_argument("id")
    c_delete.add_argument("--confirm", default=None)

    c_run = connector_sub.add_parser("run", help="executa uma capacidade do conector (exige --confirm)")
    c_run.add_argument("id")
    c_run.add_argument("capability")
    c_run.add_argument("--confirm", default=None)

    c_audit = connector_sub.add_parser("audit", help="mostra o log de auditoria de conectores")
    c_audit.add_argument("--connector", default=None)

    schedule_p = sub.add_parser("schedule", help="gerencia agendas locais de conectores (PRD-004)")
    schedule_sub = schedule_p.add_subparsers(dest="schedule_command", required=True)

    sc_set = schedule_sub.add_parser("set", help="cria uma agenda para uma capacidade read/notify (exige --confirm)")
    sc_set.add_argument("connector_id")
    sc_set.add_argument("capability")
    sc_set.add_argument("--every", type=int, required=True, dest="every_minutes", metavar="MINUTOS")
    sc_set.add_argument("--confirm", default=None)

    schedule_sub.add_parser("list", help="lista agendas")

    sc_disable = schedule_sub.add_parser("disable", help="desabilita uma agenda (exige --confirm)")
    sc_disable.add_argument("id")
    sc_disable.add_argument("--confirm", default=None)

    scheduler_p = sub.add_parser("scheduler", help="roda agendas devidas (uso: acionado pelo cron do sistema)")
    scheduler_sub = scheduler_p.add_subparsers(dest="scheduler_command", required=True)
    scheduler_sub.add_parser("run", help="executa agora, uma única vez, as agendas devidas")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "chat":
            return cmd_chat()
        if args.command == "ask":
            return cmd_ask(args.prompt)
        if args.command == "memory":
            if args.memory_command == "list":
                return cmd_memory_list(args.status)
            if args.memory_command == "show":
                return cmd_memory_show(args.id)
            if args.memory_command == "search":
                return cmd_memory_search(args.query)
            if args.memory_command == "correct":
                return cmd_memory_correct(args.id, args.content)
            if args.memory_command == "forget":
                return cmd_memory_forget(args.id, args.confirm)
            if args.memory_command == "block":
                return cmd_memory_block(args.id)
            if args.memory_command == "learning":
                return cmd_memory_learning(args.mode)
        if args.command == "session":
            if args.session_command == "list":
                return cmd_session_list()
            if args.session_command == "show":
                return cmd_session_show(args.id)
            if args.session_command == "delete":
                return cmd_session_delete(args.id, args.confirm)
        if args.command == "knowledge":
            if args.knowledge_command == "list":
                return cmd_knowledge_list()
            if args.knowledge_command == "show":
                return cmd_knowledge_show(args.id)
            if args.knowledge_command == "forget":
                return cmd_knowledge_forget(args.id, args.confirm)
        if args.command == "vault":
            if args.vault_command == "init":
                return cmd_vault_init(args.key_provider)
            if args.vault_command == "status":
                return cmd_vault_status()
            if args.vault_command == "lock":
                return cmd_vault_lock()
            if args.vault_command == "rotate-key":
                return cmd_vault_rotate_key(args.confirm)
        if args.command == "secret":
            if args.secret_command == "set":
                return cmd_secret_set(args.label, args.stdin)
            if args.secret_command == "list":
                return cmd_secret_list()
            if args.secret_command == "delete":
                return cmd_secret_delete(args.id, args.confirm)
            if args.secret_command == "audit":
                return cmd_secret_audit()
        if args.command == "connector":
            if args.connector_command == "list":
                return cmd_connector_list()
            if args.connector_command == "show":
                return cmd_connector_show(args.id)
            if args.connector_command == "create-demo":
                return cmd_connector_create_demo(args.nome)
            if args.connector_command == "create":
                return cmd_connector_create(args.provider, args.secret_id, args.confirm)
            if args.connector_command == "enable":
                return cmd_connector_enable(args.id, args.confirm)
            if args.connector_command == "disable":
                return cmd_connector_disable(args.id, args.confirm)
            if args.connector_command == "delete":
                return cmd_connector_delete(args.id, args.confirm)
            if args.connector_command == "run":
                return cmd_connector_run(args.id, args.capability, args.confirm)
            if args.connector_command == "audit":
                return cmd_connector_audit(args.connector)
        if args.command == "schedule":
            if args.schedule_command == "set":
                return cmd_schedule_set(args.connector_id, args.capability, args.every_minutes, args.confirm)
            if args.schedule_command == "list":
                return cmd_schedule_list()
            if args.schedule_command == "disable":
                return cmd_schedule_disable(args.id, args.confirm)
        if args.command == "scheduler":
            if args.scheduler_command == "run":
                return cmd_scheduler_run()
    except InvalidIdError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2

    parser.error("comando inválido")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
