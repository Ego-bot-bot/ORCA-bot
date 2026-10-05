"""
bot_main.py

Главный файл бота: VK LongPoll, команды, watcher, управление worker'ом.

Мульти-пользовательский режим:
- Любой, кто напишет боту /start, добавляется в subscribers.json.
- Все команды доступны всем подписчикам.
- Уведомления о расчётах рассылаются всем подписчикам.
"""

import json
import random
import threading
import time
from datetime import datetime
from pathlib import Path

import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType

from bot_config import (
    VK_TOKEN, VK_GROUP_ID, VK_USER_ID, WORK_DIR, WATCH_INTERVAL_SEC,
)
from bot_parser import InpFile, parse_inp, update_inp
from bot_validator import validate_params, format_messages
from bot_worker import OrcaWorker
from bot_vocab import (
    TASKS, THEORIES, BASES, SOLVENTS, MODELS, DISPERSIONS,
    CHARGE_HELP, MULT_HELP,
)


# ============================================================
# ===================== ГЛОБАЛЬНОЕ ===========================
# ============================================================

worker: OrcaWorker | None = None
vk = None
longpoll = None

WORK_PATH = Path(WORK_DIR)
SUBSCRIBERS_FILE = Path(__file__).parent / "subscribers.json"

_known_files: set[str] = set()

# Файлы, которые ORCA создаёт временно — не считаем их новыми
IGNORE_STEMS = {
    "scfgrad", "scfhess", "scfopt", "escf",
    "mp2nat", "cis", "ccsd", "trah",
    "mp2grad", "scfgrad_mp2", "scfgrad_trh",
}


def _is_temp_inp(name: str) -> bool:
    stem_lower = Path(name).stem.lower()
    if stem_lower in IGNORE_STEMS:
        return True
    return any(s in stem_lower for s in IGNORE_STEMS)


# ============================================================
# ==================== ПОДПИСЧИКИ ============================
# ============================================================

def load_subscribers() -> set[int]:
    """Загружает список user_id из subscribers.json."""
    if not SUBSCRIBERS_FILE.exists():
        # Первый запуск — добавляем владельца (VK_USER_ID)
        initial = {VK_USER_ID} if VK_USER_ID else set()
        save_subscribers(initial)
        return initial

    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Поддерживаем формат {"users": [...]} или [...]
        if isinstance(data, dict):
            ids = data.get("users", [])
        else:
            ids = data
        return set(int(x) for x in ids)
    except Exception as e:
        print(f"[subs] Ошибка чтения subscribers.json: {e}")
        return {VK_USER_ID} if VK_USER_ID else set()


def save_subscribers(ids: set[int]):
    """Сохраняет список user_id."""
    try:
        with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"users": sorted(ids)},
                f, indent=2, ensure_ascii=False,
            )
    except Exception as e:
        print(f"[subs] Ошибка сохранения: {e}")


# Глобальный набор подписчиков
_subscribers: set[int] = set()


def is_subscribed(user_id: int) -> bool:
    return user_id in _subscribers


def subscribe(user_id: int) -> bool:
    """Добавляет пользователя. Возвращает True, если был новый."""
    if user_id in _subscribers:
        return False
    _subscribers.add(user_id)
    save_subscribers(_subscribers)
    print(f"[subs] + подписчик: {user_id} (всего {len(_subscribers)})")
    return True


def unsubscribe(user_id: int) -> bool:
    """Удаляет пользователя. Возвращает True, если был удалён."""
    if user_id not in _subscribers:
        return False
    _subscribers.discard(user_id)
    save_subscribers(_subscribers)
    print(f"[subs] - подписчик: {user_id} (всего {len(_subscribers)})")
    return True


# ============================================================
# ==================== ОТПРАВКА В VK =========================
# ============================================================

def send_vk_to(user_id: int, text: str):
    """Отправить сообщение конкретному пользователю."""
    try:
        vk.messages.send(
            user_id=user_id,
            message=text,
            random_id=random.randint(1, 2**31 - 1),
        )
    except Exception as e:
        print(f"[bot] Не отправить user {user_id}: {e}")


def broadcast(text: str):
    """Разослать сообщение всем подписчикам."""
    if not _subscribers:
        print("[bot] Нет подписчиков для рассылки")
        return
    for uid in list(_subscribers):
        send_vk_to(uid, text)


def notify_worker(subject: str, body: str):
    """Callback для worker'а — рассылаем всем подписчикам."""
    broadcast(f"🔔 {subject}\n\n{body}")


# ============================================================
# ==================== WATCHER ===============================
# ============================================================

def watcher_loop():
    global _known_files

    _known_files = {
        f.name for f in WORK_PATH.glob("*.inp")
        if f.is_file() and not _is_temp_inp(f.name)
    }
    print(f"[watcher] Стартовый список: {len(_known_files)} .inp")

    while True:
        try:
            time.sleep(WATCH_INTERVAL_SEC)

            current = {
                f.name for f in WORK_PATH.glob("*.inp")
                if f.is_file() and not _is_temp_inp(f.name)
            }
            new_files = current - _known_files
            removed_files = _known_files - current

            if new_files:
                for name in sorted(new_files):
                    broadcast(
                        f"📄 Новый .inp: {name}\n"
                        f"Посмотреть: /show {name}\n"
                        f"Проверить:   /set {name}"
                    )
                _known_files = current

            if removed_files:
                for name in sorted(removed_files):
                    broadcast(f"🗑 Удалён .inp: {name}")
                _known_files = current

        except Exception as e:
            print(f"[watcher] Ошибка: {e}")


# ============================================================
# ==================== РАЗБОР ПАРАМЕТРА =====================
# ============================================================

PARAM_ALIASES = {
    "task":         "task",
    "тип":          "task",
    "theory":       "theory",
    "метод":        "theory",
    "method":       "theory",
    "basis":        "basis",
    "базис":        "basis",
    "disp":         "dispersion",
    "dispersion":   "dispersion",
    "dispers":      "dispersion",
    "solvent":      "solvent",
    "растворитель": "solvent",
    "model":        "model",
    "модель":       "model",
    "charge":       "charge",
    "заряд":        "charge",
    "mult":         "multiplicity",
    "multiplicity": "multiplicity",
    "мультиплет":   "multiplicity",
}


# ============================================================
# ==================== ФОРМАТИРОВАНИЕ =======================
# ============================================================

def fmt_task_list() -> str:
    lines = ["⚙️ **Типы задач**", ""]
    for key, info in TASKS.items():
        lines.append(f"**{key}** — {info['desc']}")
        lines.append(f"  Когда: {info['when']}")
        lines.append(f"  Стоимость: {info['cost']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def fmt_theory_list() -> str:
    lines = ["🧪 **Методы (теория)**", ""]
    for key, info in THEORIES.items():
        comp = " [композитный]" if info.get("composite") else ""
        lines.append(f"**{key}**{comp} — {info['desc']}")
        lines.append(f"  Когда: {info['when']}")
        lines.append(f"  Стоимость: {info['cost']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def fmt_basis_list() -> str:
    lines = ["📐 **Базисы**", ""]
    for key, info in BASES.items():
        lines.append(f"**{key}** — {info['desc']}")
        lines.append(f"  Когда: {info['when']}")
        lines.append(f"  Качество: {info['quality']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def fmt_solvent_list() -> str:
    lines = ["💧 **Растворители**", ""]
    for key, desc in SOLVENTS.items():
        lines.append(f"• `{key}` — {desc}")
    return "\n".join(lines)


def fmt_model_list() -> str:
    lines = ["🌊 **Модели сольватации**", ""]
    for key, desc in MODELS.items():
        lines.append(f"**{key}** — {desc}")
    return "\n".join(lines)


def fmt_disp_list() -> str:
    lines = ["🌀 **Dispersion corrections**", ""]
    for key, desc in DISPERSIONS.items():
        name = key if key else "(пусто)"
        lines.append(f"**{name}** — {desc}")
    return "\n".join(lines)


def fmt_file_status(name: str) -> str:
    path = WORK_PATH / name
    out_path = path.with_suffix(".out")
    bak = path.with_suffix(".inp.bak")

    exists_inp = path.exists()
    exists_out = out_path.exists()
    has_bak = bak.exists()

    if exists_out:
        icon = "✅"
        status = "есть .out"
    elif has_bak:
        icon = "✏️"
        status = "был изменён"
    else:
        icon = "○"
        status = "ожидает"

    size_kb = path.stat().st_size / 1024 if exists_inp else 0
    return f"{icon} `{name}` — {status} ({size_kb:.1f} KB)"


def fmt_help() -> str:
    return (
        "🤖 **ORCA Bot — команды**\n\n"
        "**Управление подпиской:**\n"
        "/start — подписаться на уведомления\n"
        "/unsubscribe — отписаться\n"
        "/whoami — показать ваш ID\n"
        "\n"
        "**Просмотр:**\n"
        "/list — все .inp в папке\n"
        "/show <file> — показать содержимое\n"
        "/status — что считается сейчас\n"
        "/queue — показать очередь\n"
        "\n"
        "**Настройка:**\n"
        "/set <file> <параметр> <значение> — изменить .inp\n"
        "  Параметры: task, theory, basis, dispersion, solvent,\n"
        "             model, charge, mult\n"
        "/delete <file> — удалить .inp и .out\n"
        "\n"
        "**Запуск:**\n"
        "/run <file> — запустить один файл\n"
        "/run all — запустить все ожидающие\n"
        "/stop — остановить текущий расчёт\n"
        "/stop all — остановить + очистить очередь\n"
        "/stop <file> — убрать файл из очереди\n"
        "\n"
        "**Справка:**\n"
        "/methods — список методов\n"
        "/bases — список базисов\n"
        "/tasks — типы задач\n"
        "/solvents — растворители\n"
        "/models — модели сольватации\n"
        "/disp — dispersion corrections\n"
        "/charge — что такое заряд\n"
        "/mult — что такое мультиплетность\n"
        "/help — это сообщение"
    )


# ============================================================
# ==================== ОБРАБОТКА КОМАНД =====================
# ============================================================

def cmd_list() -> str:
    files = sorted(WORK_PATH.glob("*.inp"))
    files = [f for f in files
             if f.is_file() and not f.name.endswith(".bak")
             and not _is_temp_inp(f.name)]
    if not files:
        return f"📁 Папка пуста: {WORK_PATH}"

    lines = [f"📁 **Файлы в {WORK_PATH.name}/**", ""]
    for f in files:
        lines.append(fmt_file_status(f.name))

    pending = sum(1 for f in files
                  if not f.with_suffix(".out").exists())
    lines.append("")
    lines.append(f"Всего: {len(files)}, ожидают: {pending}")
    return "\n".join(lines)


def cmd_show(name: str) -> str:
    path = WORK_PATH / name
    if not path.exists():
        return f"❌ Файл не найден: {name}"

    try:
        inp = InpFile(path)
        params = inp.get_params()
        content = "".join(inp.lines)

        if len(content) > 2500:
            content = content[:2500] + "\n... (обрезано)"

        lines = [
            f"📄 **{name}**",
            "",
            "**Текущие параметры:**",
            f"  task:       {params.get('task') or '—'}",
            f"  theory:     {params.get('theory') or '—'}",
            f"  basis:      {params.get('basis') or '—'}",
            f"  dispersion: {params.get('dispersion') or '—'}",
            f"  solvent:    {params.get('solvent') or '—'}",
            f"  model:      {params.get('model') or '—'}",
            f"  charge:     {params.get('charge')}",
            f"  mult:       {params.get('multiplicity')}",
            "",
            "**Содержимое:**",
            "```",
            content,
            "```",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Ошибка чтения: {e}"


def cmd_status() -> str:
    if not worker:
        return "⚠️ Worker не запущен"

    cur = worker.current_task()
    qsize = worker.queue_size()

    if cur:
        return f"⏳ **Считается:** {cur}\nВ очереди: {qsize}"
    elif qsize > 0:
        return f"○ Очередь: {qsize} задач, сейчас простаивает"
    return "✅ Нет активных расчётов"


def cmd_queue() -> str:
    if not worker:
        return "⚠️ Worker не запущен"
    items = worker.list_queue()
    if not items:
        return "Очередь пуста"
    lines = [f"📋 **Очередь ({len(items)}):**", ""]
    for i, name in enumerate(items, 1):
        lines.append(f"{i}. {name}")
    return "\n".join(lines)


def cmd_set(args: list[str]) -> str:
    if not args:
        return "Использование: /set <file> <param> <value>"

    name = args[0]
    path = WORK_PATH / name

    if not path.exists():
        return f"❌ Файл не найден: {name}"

    if len(args) == 1:
        params = parse_inp(path)
        msgs = validate_params(params)
        return f"🔍 **Проверка {name}:**\n\n{format_messages(msgs)}"

    if len(args) < 3:
        return "Использование: /set <file> <param> <value>"

    param_raw = args[1].lower()
    value = " ".join(args[2:])

    param = PARAM_ALIASES.get(param_raw)
    if not param:
        return (f"❌ Неизвестный параметр: {param_raw}\n"
                f"Доступные: task, theory, basis, dispersion, "
                f"solvent, model, charge, mult")

    if param == "charge":
        try:
            value = int(value)
        except ValueError:
            return "❌ charge должен быть целым числом"
    elif param == "multiplicity":
        try:
            value = int(value)
        except ValueError:
            return "❌ mult должен быть целым числом"
    elif param == "dispersion":
        if value.lower() in ("none", "нет", "-", ""):
            value = ""

    kwargs = {param: value}

    try:
        update_inp(path, **kwargs)
    except Exception as e:
        return f"❌ Ошибка применения: {e}"

    params = parse_inp(path)
    msgs = validate_params(params)

    return (f"✏️ **{name}: {param} = {value}**\n\n"
            f"{format_messages(msgs)}")


def cmd_delete(name: str) -> str:
    path = WORK_PATH / name
    if not path.exists():
        return f"❌ Файл не найден: {name}"

    deleted = []
    for suffix in (".inp", ".out", ".inp.bak"):
        p = path.with_suffix(suffix)
        if p.exists():
            try:
                p.unlink()
                deleted.append(p.name)
            except Exception as e:
                return f"❌ Ошибка удаления {p.name}: {e}"

    return f"🗑 Удалено: {', '.join(deleted)}"


def cmd_run(args: list[str]) -> str:
    if not worker:
        return "⚠️ Worker не запущен"

    if not args:
        return "Использование: /run <file> или /run all"

    if args[0].lower() == "all":
        files = sorted(WORK_PATH.glob("*.inp"))
        files = [f for f in files
                 if f.is_file() and not f.name.endswith(".bak")
                 and not _is_temp_inp(f.name)]

        pending = [f for f in files
                   if not f.with_suffix(".out").exists()]
        if not pending:
            return "Нет ожидающих .inp"

        for f in pending:
            worker.enqueue(f)
        return f"🚀 В очередь добавлено {len(pending)} файлов"

    name = args[0]
    path = WORK_PATH / name
    if not path.exists():
        return f"❌ Файл не найден: {name}"

    params = parse_inp(path)
    msgs = validate_params(params)
    has_error = any(m["level"] == "error" for m in msgs)

    if has_error:
        return (f"❌ **Отказано в запуске — есть ошибки:**\n\n"
                f"{format_messages(msgs)}")

    worker.enqueue(path)

    warn = ""
    if any(m["level"] == "warning" for m in msgs):
        warn = "\n⚠️ Есть предупреждения, но запуск разрешён"

    return f"🚀 Файл {name} добавлен в очередь{warn}"


def cmd_stop(args: list[str]) -> str:
    if not worker:
        return "⚠️ Worker не запущен"

    if not args:
        if worker.stop_current():
            return "⏹ Текущий расчёт остановлен"
        return "Нет активных расчётов"

    keyword = args[0].lower()

    if keyword == "all":
        killed = worker.stop_current()
        cleared = worker.clear_queue()

        parts = []
        if killed:
            parts.append("текущий расчёт убит")
        if cleared:
            parts.append(f"из очереди удалено {cleared} задач")
        if not parts:
            return "Нечего останавливать"
        return "⏹ " + ", ".join(parts)

    name = args[0]
    if not name.endswith(".inp"):
        name += ".inp"

    removed = worker.remove_from_queue(name)
    if removed:
        return f"🗑 Из очереди удалено: {name}"
    return f"❌ {name} в очереди не найдено"


# ============================================================
# =========== ПОДПИСКА И СЛУЖЕБНЫЕ КОМАНДЫ ==================
# ============================================================

def cmd_subscribe(user_id: int) -> str:
    """Обрабатывает /start или /subscribe."""
    added = subscribe(user_id)
    if added:
        return (
            "✅ Вы подписаны на уведомления.\n\n"
            "Теперь вы будете получать:\n"
            "  • Уведомления о новых .inp\n"
            "  • Старт/финиш каждого расчёта\n"
            "  • Ошибки, зависания, таймауты\n\n"
            "Напишите /help для списка команд."
        )
    return (
        "Вы уже подписаны.\n"
        "Команды: /help\n"
        "Отписка: /unsubscribe"
    )


def cmd_unsubscribe(user_id: int) -> str:
    if unsubscribe(user_id):
        return "👋 Вы отписаны. Уведомления больше не придут."
    return "Вы не были подписаны."


def cmd_whoami(user_id: int) -> str:
    return (
        f"🆔 **Ваш VK user_id:** `{user_id}`\n"
        f"Статус подписки: "
        f"{'✅ активна' if is_subscribed(user_id) else '❌ нет'}"
    )


def cmd_subscribers_list(user_id: int) -> str:
    """Список подписчиков — только для владельца."""
    if user_id != VK_USER_ID:
        return "❌ Команда доступна только владельцу бота."

    if not _subscribers:
        return "Подписчиков нет."

    lines = [f"👥 **Подписчики ({len(_subscribers)}):**", ""]
    for uid in sorted(_subscribers):
        mark = " (владелец)" if uid == VK_USER_ID else ""
        lines.append(f"• `{uid}`{mark}")
    return "\n".join(lines)


# ============================================================
# ==================== ГЛАВНЫЙ ДИСПЕТЧЕР ====================
# ============================================================

def handle_command(text: str, user_id: int) -> str:
    """
    Разбирает команду. Возвращает ответ (строку для VK).
    user_id нужен для подписки/отписки и служебных команд.
    """
    text = text.strip()
    if not text.startswith("/"):
        return ""

    parts = text[1:].split()
    cmd = parts[0].lower()
    args = parts[1:]

    # Служебные команды
    if cmd in ("start", "subscribe"):
        return cmd_subscribe(user_id)
    if cmd == "unsubscribe":
        return cmd_unsubscribe(user_id)
    if cmd == "whoami":
        return cmd_whoami(user_id)
    if cmd == "subscribers":
        return cmd_subscribers_list(user_id)

    # Проверка подписки для остальных команд
    if not is_subscribed(user_id):
        return (
            "🔒 Вы не подписаны.\n"
            "Напишите /start чтобы получить доступ."
        )

    if cmd == "help":
        return fmt_help()
    if cmd == "list":
        return cmd_list()
    if cmd == "show":
        if not args:
            return "Использование: /show <file>"
        return cmd_show(args[0])
    if cmd == "status":
        return cmd_status()
    if cmd == "queue":
        return cmd_queue()
    if cmd == "set":
        return cmd_set(args)
    if cmd == "delete":
        if not args:
            return "Использование: /delete <file>"
        return cmd_delete(args[0])
    if cmd == "run":
        return cmd_run(args)
    if cmd == "stop":
        return cmd_stop(args)
    if cmd == "methods":
        return fmt_theory_list()
    if cmd == "bases":
        return fmt_basis_list()
    if cmd == "tasks":
        return fmt_task_list()
    if cmd == "solvents":
        return fmt_solvent_list()
    if cmd == "models":
        return fmt_model_list()
    if cmd == "disp":
        return fmt_disp_list()
    if cmd == "charge":
        return CHARGE_HELP
    if cmd == "mult":
        return MULT_HELP

    return f"❓ Неизвестная команда: /{cmd}\nНапишите /help"


# ============================================================
# ========================= MAIN =============================
# ============================================================

def main():
    global vk, longpoll, worker, _subscribers

    print("=" * 60)
    print("ORCA Bot запускается (мульти-режим)...")
    print("=" * 60)

    if not WORK_PATH.exists():
        print(f"[!] Папка не найдена: {WORK_PATH}")
        return
    print(f"[+] Рабочая папка: {WORK_PATH}")

    # Загружаем подписчиков
    _subscribers = load_subscribers()
    print(f"[+] Подписчиков: {len(_subscribers)}")

    # VK
    try:
        vk_session = vk_api.VkApi(token=VK_TOKEN)
        vk = vk_session.get_api()
        longpoll = VkLongPoll(vk_session, group_id=VK_GROUP_ID)
        print(f"[+] VK готов (group={VK_GROUP_ID})")
    except Exception as e:
        print(f"[!] Ошибка VK: {e}")
        return

    # Worker
    worker = OrcaWorker(notify_callback=notify_worker)
    worker.start()
    print("[+] Worker запущен")

    # Watcher
    t_watch = threading.Thread(target=watcher_loop, daemon=True)
    t_watch.start()
    print(f"[+] Watcher запущен (каждые {WATCH_INTERVAL_SEC} сек)")

    # Приветствие подписчикам
    broadcast(
        "🤖 ORCA Bot запущен\n\n"
        f"Рабочая папка: {WORK_PATH.name}\n"
        f"Подписчиков: {len(_subscribers)}\n"
        "Напишите /help для списка команд."
    )

    # LongPoll
    print("[+] Слушаю команды... (Ctrl+C для выхода)")
    try:
        for event in longpoll.listen():
            if event.type == VkEventType.MESSAGE_NEW and event.to_me:
                user_id = event.user_id
                text = event.text
                print(f"[bot] От user {user_id}: {text!r}")
                reply = handle_command(text, user_id)
                if reply:
                    send_vk_to(user_id, reply)
    except KeyboardInterrupt:
        print("\n[bot] Остановка по Ctrl+C")
    finally:
        if worker:
            worker.stop()
        print("[bot] Завершено")


if __name__ == "__main__":
    main()