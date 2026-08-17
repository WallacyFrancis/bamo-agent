"""Caminhos centrais do projeto, compartilhados por todos os módulos de core/."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEMORY_CORE = ROOT / "memory" / "CORE.md"
MEMORY_FACTS_DIR = ROOT / "memory" / "facts"
KNOWLEDGE_OKF_DIR = ROOT / "knowledge" / "okf"
SESSIONS_DIR = ROOT / "sessions"
SKILLS_DIR = ROOT / "skills"
SETTINGS_PATH = ROOT / "settings.local.json"
SECRETS_DIR = ROOT / "secrets"
VAULT_PATH = SECRETS_DIR / "vault.enc"
CONNECTORS_DIR = ROOT / "connectors"
SCHEDULES_DIR = ROOT / "schedules"
CONNECTOR_AUDIT_DIR = ROOT / "connector-audit"
EXECUTORS_DIR = ROOT / "executors"
