"""Renderização central de terminal (PRD-007) — cor, arte, cards, menus e
confirmações para `bamo chat` e comandos relacionados a status/alertas.

Isolamento deliberado (seção 7): este módulo não importa `vault`,
`dispatcher`, executores, rede nem `agy_runtime`. Só recebe texto e dados
já seguros/redigidos por quem chama (ex.: `core/alerts.py`,
`core/operations.py`) — nunca decide o que é seguro mostrar, só como
mostrar. Nenhuma regra de segurança muda aqui: `confirmation()` só exibe o
comando `--confirm <id>` que o chamador já exige; escolher algo num
`menu()` nunca substitui essa confirmação (seção 5).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from typing import Any

from .atomic import write_json_atomic
from .paths import SETTINGS_PATH

# Remove sequências de escape ANSI (CSI, OSC, e outras introduzidas por ESC)
# e caracteres de controle (exceto \n e \t) de qualquer texto antes de
# imprimir — obrigatório para conteúdo que pode vir de fora (nome de
# repositório, resumo de executor, erro de rede) e nunca pode manipular o
# terminal do usuário (seção 7.1: "Remover escapes ANSI e controles
# externos antes de renderizar").
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _visible_len(text: str) -> int:
    """Comprimento em colunas de terminal, ignorando sequências ANSI —
    todo cálculo de padding de caixa/coluna usa isto, nunca `len()` direto,
    para bordas nunca desalinharem por causa de código de cor embutido."""
    return len(_ANSI_RE.sub("", text))

_COLORS = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "green": "\x1b[32m",
    "cyan": "\x1b[36m",
    "yellow": "\x1b[33m",
    "red": "\x1b[31m",
    "magenta": "\x1b[35m",
}

_NARROW_WIDTH = 44
_COLOR_MODES = ("auto", "always", "never")

_state: dict[str, Any] = {"color_mode": "auto", "plain": False}

_ART = [
    "       ( (",
    "        ) )",
    "     .------.",
    "     |      |]",
    "     |      |",
    "     '------'",
    "       B A M O",
]

_NOTICE_STYLES = {
    "success": ("✓ Pronto", "[OK]", "green"),
    "info": ("i Informação", "[i]", "cyan"),
    "warning": ("! Atenção", "[!]", "yellow"),
    "error": ("× Erro", "[x]", "red"),
    "alert": ("! ALERTA", "[ALERTA]", "yellow"),
    "confirm": ("? Confirmar", "[?]", "magenta"),
}


def sanitize(value: Any) -> str:
    """Único ponto que decide se um texto é seguro para ir ao terminal —
    remove ANSI e controles, nunca trunca nem altera caracteres normais
    (Unicode incluso), então IDs e comandos de confirmação continuam
    exatos (seção 7.6)."""
    text = value if isinstance(value, str) else str(value)
    text = _ANSI_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    return text


# --- configuração de cor/modo -------------------------------------------


def _load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_settings_patch(patch: dict[str, Any]) -> None:
    """Lê-modifica-grava: nunca apaga outras chaves já em
    settings.local.json (ex.: `learning_enabled`) — só atualiza as suas."""
    data = _load_settings()
    data.update(patch)
    write_json_atomic(SETTINGS_PATH, data)


def load_display_settings() -> tuple[str, bool]:
    data = _load_settings()
    color_mode = data.get("color_mode")
    if color_mode not in _COLOR_MODES:
        color_mode = "auto"
    plain = bool(data.get("plain", False))
    return color_mode, plain


def save_display_settings(color_mode: str, plain: bool) -> None:
    if color_mode not in _COLOR_MODES:
        color_mode = "auto"
    _save_settings_patch({"color_mode": color_mode, "plain": bool(plain)})


def configure(color_mode: str = "auto", plain: bool = False) -> None:
    _state["color_mode"] = color_mode if color_mode in _COLOR_MODES else "auto"
    _state["plain"] = bool(plain)


def _is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _stream_is_tty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def is_plain() -> bool:
    return bool(_state["plain"])


def should_render_rich() -> bool:
    """Verdadeiro só quando cards/menus/arte fazem sentido: sem `--plain` e
    com stdout interativo (seção 7.3: "--plain e saída sem TTY não usam
    arte, cor ou prompts de seleção"). Comandos não interativos (pipe,
    script, `--plain`) caem para o texto simples de sempre."""
    return not _state["plain"] and _is_tty()


def _color_enabled_for(stream: Any) -> bool:
    """`--color always` só força cor quando o stream de destino é mesmo um
    terminal interativo — sem isso, `always` vazava ANSI em pipe, arquivo
    de log ou saída de cron, que nunca deveriam carregar escape algum.
    `stream` importa porque `notice()` escreve em stdout ou stderr
    conforme o tipo de aviso, e os dois podem ter "interatividade"
    diferente (ex.: stdout redirecionado para um arquivo, stderr no
    terminal)."""
    if _state["plain"]:
        return False
    mode = _state["color_mode"]
    if mode == "never":
        return False
    if not _stream_is_tty(stream):
        return False
    if mode == "always":
        return True
    # auto
    return os.environ.get("NO_COLOR") is None


def _color_enabled() -> bool:
    return _color_enabled_for(sys.stdout)


def _c(name: str, text: str, stream: Any = None) -> str:
    if not _color_enabled_for(stream if stream is not None else sys.stdout):
        return text
    code = _COLORS.get(name)
    if not code:
        return text
    return f"{code}{text}{_COLORS['reset']}"


def _supports_unicode() -> bool:
    if _state["plain"]:
        return False
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower()
    return "utf" in encoding


def _use_boxes() -> bool:
    if not should_render_rich():
        return False
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    return width >= _NARROW_WIDTH


# --- banner (moldura TrueColor + pixel art em half-blocks) ---------------

_BANNER_ORANGE = (249, 115, 22)
_BANNER_SOLAR = (255, 215, 0)
_BANNER_AMBER = (230, 160, 20)
_BANNER_WHITE = (255, 255, 255)
_BANNER_CREME = (240, 235, 220)
_BANNER_COFFEE = (90, 50, 20)
_BANNER_GOLD = (230, 180, 40)
_BANNER_SHADOW = (140, 95, 15)
_BANNER_MUTED = (150, 150, 150)

_BANNER_WIDTH = 76  # colunas totais, bordas inclusas (faixa pedida: 74-78)
_BANNER_TITLE = "BAMO – AGENT"

_CUP_W = 18


def _cup_row(*placements: tuple[int, str]) -> str:
    cells = [" "] * _CUP_W
    for col, ch in placements:
        cells[col] = ch
    return "".join(cells)


# Cada string é uma linha de "pixels" (1 caractere = 1 pixel); duas linhas
# formam uma linha de half-blocks (`▀`) na hora de renderizar — ver
# `_half_block_lines()`.
_CUP_PIXELS = [
    _cup_row((6, "."), (9, ","), (12, ".")),  # vapor
    _cup_row((6, "."), (9, ","), (12, ".")),
    _cup_row((5, "."), (9, ","), (13, ".")),
    _cup_row((5, "."), (9, ","), (13, ".")),
    _cup_row((6, "."), (9, ","), (12, ".")),
    _cup_row((6, "."), (9, ","), (12, ".")),
    _cup_row(*[(c, "W") for c in (4, 14)], *[(c, "C") for c in range(5, 14)]),  # borda + café
    _cup_row(*[(c, "Y") for c in range(4, 15)], (15, "W")),  # corpo — ouro
    _cup_row(*[(c, "W") for c in range(4, 15)], (16, "W")),  # corpo — branco
    _cup_row(*[(c, "Y") for c in range(4, 15)], (17, "W")),
    _cup_row(*[(c, "W") for c in range(4, 15)], (17, "W")),
    _cup_row(*[(c, "Y") for c in range(4, 15)], (16, "W")),
    _cup_row(*[(c, "W") for c in range(4, 15)], (15, "W")),
    _cup_row(*[(c, "Y") for c in range(5, 14)]),  # base
    _cup_row(*[(c, "B") for c in range(6, 13)]),
    _cup_row(*[(c, "B") for c in range(7, 12)]),
]

_CUP_LEGEND: dict[str, tuple[int, int, int]] = {
    ".": _BANNER_SOLAR,
    ",": _BANNER_AMBER,
    "W": _BANNER_WHITE,
    "Y": _BANNER_GOLD,
    "C": _BANNER_COFFEE,
    "B": _BANNER_SHADOW,
}


def _rgb_fg(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"\x1b[38;2;{r};{g};{b}m"


def _rgb_bg(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"\x1b[48;2;{r};{g};{b}m"


def _half_block_lines(pixels: list[str], legend: dict[str, tuple[int, int, int]]) -> list[str]:
    """Cada 2 linhas de pixels viram 1 linha de terminal: `▀` com cor de
    frente = pixel de cima e cor de fundo = pixel de baixo (técnica de
    half-blocks). Quando só um dos dois pixels existe, usa `▀`/`▄` sem cor
    de fundo, para não pintar um retângulo sólido onde deveria haver
    transparência (fundo do terminal)."""
    lines: list[str] = []
    reset = _COLORS["reset"]
    for i in range(0, len(pixels), 2):
        top, bottom = pixels[i], pixels[i + 1]
        parts: list[str] = []
        for col in range(len(top)):
            top_rgb = legend.get(top[col])
            bottom_rgb = legend.get(bottom[col])
            if top_rgb and bottom_rgb:
                parts.append(f"{_rgb_fg(top_rgb)}{_rgb_bg(bottom_rgb)}▀{reset}")
            elif top_rgb:
                parts.append(f"{_rgb_fg(top_rgb)}▀{reset}")
            elif bottom_rgb:
                parts.append(f"{_rgb_fg(bottom_rgb)}▄{reset}")
            else:
                parts.append(" ")
        lines.append("".join(parts))
    return lines


def _pad_visible(text: str, width: int) -> str:
    return text + " " * max(0, width - _visible_len(text))


def _center_visible(text: str, width: int) -> str:
    gap = max(0, width - _visible_len(text))
    left = gap // 2
    return " " * left + text + " " * (gap - left)


def _banner_border(text: str) -> str:
    return f"{_rgb_fg(_BANNER_ORANGE)}{text}{_COLORS['reset']}"


def _plain_banner() -> None:
    """Sem cor (mas ainda em modo rico): mantém a arte antiga em texto puro
    — half-blocks coloridos não fazem sentido sem TrueColor."""
    for line in _ART:
        print(line)
    print()
    print("  Uai, bamo trabalhar?")
    print()


def _rich_banner(*, engine: str, model: str, user_email: str, version: str) -> None:
    inner_width = _BANNER_WIDTH - 2  # entre as bordas │ │
    margin = 1
    content_width = inner_width - 2 * margin
    left_width = _CUP_W + 3  # arte + respiro até a coluna da direita
    right_width = content_width - left_width

    # topo: ╭─ BAMO – AGENT ───...───╮
    title = f"{_COLORS['bold']}{_rgb_fg(_BANNER_ORANGE)}{_BANNER_TITLE}{_COLORS['reset']}"
    prefix = "╭─ "
    suffix = " "
    dashes = _BANNER_WIDTH - len(prefix) - _visible_len(_BANNER_TITLE) - len(suffix) - 1
    print(_banner_border(prefix) + title + _banner_border(suffix + "─" * max(0, dashes) + "╮"))

    cup_lines = _half_block_lines(_CUP_PIXELS, _CUP_LEGEND)
    version_line = _center_visible(f"{_rgb_fg(_BANNER_MUTED)}Versão {version}{_COLORS['reset']}", _CUP_W)
    left_lines = cup_lines + [version_line]

    label_value = [
        ("Motor", engine),
        ("Modelo", model),
        ("Usuário", user_email),
    ]
    label_width = max(len(label) for label, _ in label_value) + 1
    meta_lines = [
        f"{_rgb_fg(_BANNER_SOLAR)}{label.ljust(label_width)}:{_COLORS['reset']} {_rgb_fg(_BANNER_CREME)}{value}{_COLORS['reset']}"
        for label, value in label_value
    ]
    top_pad = max(0, (len(left_lines) - len(meta_lines)) // 2)
    right_lines = [""] * top_pad + meta_lines
    right_lines += [""] * (len(left_lines) - len(right_lines))

    for left_text, right_text in zip(left_lines, right_lines):
        row = " " * margin + _pad_visible(left_text, left_width) + _pad_visible(right_text, right_width) + " " * margin
        print(_banner_border("│") + row + _banner_border("│"))

    print(_banner_border("╰" + "─" * (_BANNER_WIDTH - 2) + "╯"))


# --- identidade e mensagens ----------------------------------------------


def banner(
    *,
    engine: str = "Antigravity",
    model: str = "Gemini 3.1 Pro",
    user_email: str = "wallacyfrancis07@gmail.com",
    version: str = "1.0.0",
) -> None:
    if not should_render_rich():
        return
    if not _color_enabled():
        _plain_banner()
        return
    _rich_banner(engine=engine, model=model, user_email=user_email, version=version)


def session_header() -> None:
    if not should_render_rich():
        return
    width = min(shutil.get_terminal_size(fallback=(80, 24)).columns, 60)
    rule = _c("dim", "─" * width)
    print(rule)
    print("Sessão ativa • memória local • digite /ajuda para opções")
    print(rule)


def user_message(text: str) -> None:
    print(f"{_c('cyan', 'Você')} › {sanitize(text)}")


def user_prompt_label() -> str:
    """Rótulo colorido usável direto como prompt de `input()` — o próprio
    terminal ecoa o texto digitado logo em seguida, então não há
    `user_message()` duplicado no loop interativo de `bamo chat`."""
    return f"{_c('cyan', 'Você')} › "


def bamo_label() -> str:
    return "☕ Bamo" if _supports_unicode() else "[Bamo]"


def bamo_message(text: str) -> None:
    print()
    print(_c("bold", bamo_label()))
    clean = sanitize(text)
    for line in clean.splitlines() or [""]:
        print(f"│ {line}")
    print()


def system_message(text: str) -> None:
    print(f"[sistema] {sanitize(text)}")


def notice(kind: str, text: str, *, to_stderr: bool | None = None) -> None:
    rich_label, plain_label, color = _NOTICE_STYLES.get(kind, ("i Informação", "[i]", "cyan"))
    label = rich_label if _supports_unicode() else plain_label
    stream = sys.stderr if (to_stderr if to_stderr is not None else kind in ("error", "alert", "warning")) else sys.stdout
    print(f"{_c(color, label, stream)}: {sanitize(text)}", file=stream)


# --- confirmação e menu ---------------------------------------------------


def confirmation(title: str, fields: dict[str, str], command: str) -> None:
    """Só exibição — a decisão de exigir `--confirm <id>` continua inteira
    em quem chama (seção 5). Nenhum campo aqui é truncado, para nunca
    esconder um ID ou o comando exato de confirmação (seção 7.6)."""
    label = "? Confirmar" if _supports_unicode() else "[?]"
    print()
    print(_c("magenta", label))
    if title:
        print(f"  {sanitize(title)}")
    print()
    for key, value in fields.items():
        print(f"  {sanitize(str(key))}: {sanitize(str(value))}")
    print()
    print("Para confirmar, rode:")
    print(f"  {command}")
    print()


def menu(title: str, choices: list[tuple[str, str, str]]) -> str | None:
    """`choices`: lista de (chave, rótulo, descrição). Retorna a chave
    escolhida ou `None` se cancelado (`0`/`sair`/Ctrl+C/EOF — nunca tratado
    como erro), entrada inválida (número fora da faixa, texto desconhecido
    ou campo vazio — mostra orientação e não executa nada), ou sem TTY
    (seção 5: "Entrada inválida não executa nada... Menus só aparecem em
    TTY; scripts mantêm saída previsível")."""
    if not should_render_rich() or not _stdin_is_tty():
        return None
    print()
    print(f"? {sanitize(title)}")
    print()
    for i, (_key, label, desc) in enumerate(choices, start=1):
        print(f"  [{i}] {sanitize(label)}")
        if desc:
            print(f"      {sanitize(desc)}")
    print()
    try:
        raw = input(f"Escolha [1-{len(choices)}]: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    raw = raw.strip()
    lowered = raw.lower()
    if lowered in ("0", "sair"):
        return None
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(choices):
            return choices[idx - 1][0]
    else:
        for key, label, _desc in choices:
            if lowered in (key.lower(), label.lower()):
                return key
    notice("warning", f"escolha um número entre 1 e {len(choices)}, o nome da opção, 0 ou sair.")
    return None


# --- cards -----------------------------------------------------------------


def _card(title: str, lines: list[str]) -> None:
    # `title`/`lines` já vêm sanitizados de quem chama (`status_card`,
    # `alert_card`) — não sanitiza de novo aqui para não apagar cor
    # intencional (`_c(...)`) que o chamador tenha embutido no título.
    if not _use_boxes():
        print(title)
        for line in lines:
            print(f"  {line}")
        print()
        return
    header = f"┌─ {title} "
    header += "─" * max(3, 58 - len(header))
    print(header)
    for line in lines:
        print(f"│ {line}")
    print("└" + "─" * (len(header) - 1))
    print()


def _relative_past(iso_value: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_value)
    except (TypeError, ValueError):
        return sanitize(iso_value)
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 0:
        return "em breve"
    if seconds < 60:
        return "agora mesmo"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"há {minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"há {hours}h"
    days = hours // 24
    return f"há {days} dia(s)"


def _relative_future(iso_value: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_value)
    except (TypeError, ValueError):
        return sanitize(iso_value)
    seconds = (dt - datetime.now(timezone.utc)).total_seconds()
    if seconds <= 0:
        return "agora"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"em {minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"em {hours}h"
    days = hours // 24
    return f"em {days} dia(s)"


def status_card(entry: dict[str, Any]) -> None:
    """`entry` é um item de `core.operations.status()` — já livre de
    segredo, token, URL completa ou corpo de resposta (seção 6)."""
    title = f"{sanitize(entry['display_name'])} · {sanitize(entry['provider'])}"
    lines = [
        f"Conector: {sanitize(entry['connector_id'])}",
        f"Estado: {'habilitado' if entry['enabled'] else 'desabilitado'}",
    ]

    last = entry.get("last_execution")
    if last:
        if last["ok"]:
            ok_icon = _c("green", "✓" if _supports_unicode() else "OK")
        else:
            ok_icon = _c("red", "×" if _supports_unicode() else "ERRO")
        lines.append(f"Última execução: {ok_icon} {sanitize(last['status'])} · {_relative_past(last['at'])}")
    else:
        lines.append("Última execução: nunca")

    schedules = entry.get("schedules") or []
    if not schedules:
        lines.append("Agenda: nenhuma")
    for sched in schedules:
        estado = "habilitada" if sched["enabled"] else "desabilitada"
        next_due = sched.get("next_due")
        if next_due == "agora":
            proxima = "agora"
        elif next_due:
            proxima = _relative_future(next_due)
        else:
            proxima = "-"
        lines.append(f"Agenda [{sanitize(sched['capability'])}]: a cada {sched['every_minutes']} min ({estado}) · próxima {proxima}")

    open_alerts = entry.get("open_alerts") or []
    if open_alerts:
        lines.append(f"Alertas: {len(open_alerts)} aberto(s)")
        for a in open_alerts:
            lines.append(f"  - [{sanitize(a['id'])}] {sanitize(a['type'])} ({sanitize(a['severity'])})")
    else:
        lines.append("Alertas: nenhum aberto")

    _card(title, lines)


def alert_card(record: dict[str, Any]) -> None:
    """`record` é um alerta de `core.alerts` — nunca contém segredo,
    `secret_id`, token, URL completa, headers, corpo de API ou stack trace
    (seção 6); `message` já passou por `redact()` na origem."""
    severity_color = "red" if record["severity"] == "error" else "yellow"
    title = f"Alerta {sanitize(record['type'])} · {_c(severity_color, sanitize(record['severity']))}"
    lines = [
        f"Estado: {sanitize(record['state'])}",
        f"Conector: {sanitize(record['connector_id'])}",
        f"Capacidade: {sanitize(record['capability'])}",
    ]
    if record.get("schedule_id"):
        lines.append(f"Agenda: {sanitize(record['schedule_id'])}")
    lines.append(f"Ocorrências: {record['count']}")
    lines.append(f"Última ocorrência: {_relative_past(record['last_seen_at'])}")
    if record["state"] == "muted" and record.get("muted_until"):
        lines.append(f"Silenciado até: {_relative_future(record['muted_until'])}")
    lines.append(f"Mensagem: {sanitize(record['message'])}")
    _card(title, lines)
