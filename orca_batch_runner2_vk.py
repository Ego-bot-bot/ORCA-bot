"""
Автоматический последовательный запуск ORCA расчётов с контролем
зацикливания и несходимости.

Разместите файл в одной папке с input-файлами ORCA.
Настройте блок CONFIG под свою систему.
"""

import os
import re
import sys
import time
import shutil
import subprocess
import smtplib
import vk_api
import logging
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText

# ============================================================
# ======================== CONFIG ============================
# ============================================================

# Путь к исполняемому файлу ORCA
ORCA_EXE = r"C:\ORCA_6.1.0\orca.exe"   # ← ваш путь

# Папка с input-файлами (.inp)
WORK_DIR = r"C:\Users\T490\Desktop\ORCA_inp_py"  # ← ваш путь

# Очередь задач
QUEUE = [
    "Bracrolein.inp",
    "pirrol.inp",
]

# Таймауты и пороги
MAX_RUNTIME_HOURS     = 48
CHECK_INTERVAL_SEC    = 30
STALL_TIMEOUT_MIN     = 60
MAX_SCF_ITER_WARN     = 200
MAX_OPT_CYCLES_WARN   = 100

# Уведомления в VK
VK_ENABLED = True
VK_TOKEN = "vk1.a.r1SGzL9Eyzv5RBbwDxUKMSh5bybL9vVbRUD6I5LxEVr64Zsbt_ewujEYZ3L2moir7OVvX-WL2AW3YKFecYXFetbVkkpWAGZl8iyfKHQzH_ipTCJ9M5FpTh7UVQndgG9LjMmoDaGm-ZQH9IezfvwVofg1M9e9KWcne3eWxJVlX1_WmYMd9DlX6qNjBmsM6fbi6nWf67v2jbcFgreiDyzFiQ"  # ← токен
VK_GROUP_ID = 241965773  # ← ID вашего сообщества из шага 4

# Почта (отключено)
SMTP_ENABLED = False

# ============================================================
# ====================== END CONFIG ==========================
# ============================================================


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("orca_batch_runner.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ------------------------------------------------------------
# Утилиты
# ------------------------------------------------------------

def send_vk_message(subject: str, body: str):
    """Отправить уведомление в VK от имени сообщества."""
    if not VK_ENABLED:
        log.info("VK отключён, пропускаю уведомление.")
        return

    # Формируем сообщение: тема жирным + тело
    message = f"🔔 {subject}\n\n{body}"

    try:
        # Инициализация сессии VK
        vk_session = vk_api.VkApi(token=VK_TOKEN)
        vk = vk_session.get_api()

        # Отправка сообщения самому себе от имени сообщества.
        # peer_id = ваш VK user id (не group_id!). Бот отправляет сообщение вам.
        # Проще всего узнать ваш user_id через https://vk.com/dev или API.
        # Однако, если вы просто написали сообществу, можно использовать "self".
        vk.messages.send(
            user_id=288172397,  # Отправка от сообщества. Это ID группы.
            message=message,
            random_id=0,
        )
        log.info("Уведомление отправлено в VK.")
    except Exception as e:
        log.error(f"Не удалось отправить в VK: {e}")

def kill_process(proc: subprocess.Popen):
    """Корректно убить процесс ORCA и всех его потомков."""
    if proc.poll() is not None:
        return
    try:
        if sys.platform.startswith("win"):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, check=False,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.warning(f"Процесс ORCA (PID={proc.pid}) завершён принудительно.")
    except Exception as e:
        log.error(f"Ошибка при убийстве процесса: {e}")


# ------------------------------------------------------------
# Анализ выходного файла
# ------------------------------------------------------------

# Признаки явной ошибки ORCA
ERROR_PATTERNS = [
    re.compile(r"ORCA finished by error termination", re.IGNORECASE),
    re.compile(r"aborting the run", re.IGNORECASE),
    re.compile(r"Error \(ORCA\)", re.IGNORECASE),
    re.compile(r"SCF NOT CONVERGED", re.IGNORECASE),
    re.compile(r"Geometry optimization did not converge", re.IGNORECASE),
]

# Признаки нормального завершения
SUCCESS_PATTERNS = [
    re.compile(r"ORCA TERMINATED NORMALLY", re.IGNORECASE),
    re.compile(r"TOTAL RUN TIME", re.IGNORECASE),
]

# Счётчики итераций
RE_SCF_ITER    = re.compile(r"SCF ITERATION\s+(\d+)", re.IGNORECASE)
RE_OPT_CYCLE   = re.compile(r"GEOMETRY OPTIMIZATION CYCLE\s+(\d+)", re.IGNORECASE)
RE_NEB_ITER    = re.compile(r"NEB ITERATION\s+(\d+)", re.IGNORECASE)
RE_SCF_CONV    = re.compile(r"SCF CONVERGED", re.IGNORECASE)
RE_OPT_DONE    = re.compile(r"OPTIMIZATION RUN DONE", re.IGNORECASE)


class LogMonitor:
    """Следит за выходным файлом ORCA."""

    def __init__(self, out_file: Path):
        self.out_file = out_file
        self.position = 0
        self.last_size = 0
        self.last_growth_time = time.time()

    def read_new_lines(self):
        """Читает новые строки с последней позиции."""
        if not self.out_file.exists():
            return []
        try:
            with open(self.out_file, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self.position)
                lines = f.readlines()
                self.position = f.tell()
            return lines
        except Exception as e:
            log.debug(f"Ошибка чтения лога: {e}")
            return []

    def check_growth(self):
        """Проверяет, растёт ли файл. Возвращает True, если застой."""
        try:
            size = self.out_file.stat().st_size
        except FileNotFoundError:
            return False
        if size > self.last_size:
            self.last_size = size
            self.last_growth_time = time.time()
            return False
        stall_sec = time.time() - self.last_growth_time
        return stall_sec > STALL_TIMEOUT_MIN * 60

    def analyze(self, new_lines):
        """
        Анализирует новые строки.
        Возвращает: ('success' | 'error' | 'stuck' | 'ok', message)
        """
        text = "".join(new_lines)

        for p in SUCCESS_PATTERNS:
            if p.search(text):
                return "success", "ORCA завершился нормально."

        for p in ERROR_PATTERNS:
            if p.search(text):
                return "error", f"Обнаружена ошибка: {p.pattern}"

        # Счётчики итераций во всём файле (не только в новых строках)
        max_scf, max_opt, max_neb = self._scan_counters()

        if max_scf and max_scf > MAX_SCF_ITER_WARN:
            return "stuck", f"SCF не сходится: {max_scf} итераций."
        if max_opt and max_opt > MAX_OPT_CYCLES_WARN:
            return "stuck", f"Оптимизация не сходится: {max_opt} циклов."
        if max_neb and max_neb > 500:
            return "stuck", f"NEB не сходится: {max_neb} итераций."

        return "ok", ""

    def _scan_counters(self):
        """Сканирует весь файл на максимальные значения счётчиков."""
        max_scf = max_opt = max_neb = 0
        try:
            with open(self.out_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = RE_SCF_ITER.search(line)
                    if m:
                        max_scf = max(max_scf, int(m.group(1)))
                    m = RE_OPT_CYCLE.search(line)
                    if m:
                        max_opt = max(max_opt, int(m.group(1)))
                    m = RE_NEB_ITER.search(line)
                    if m:
                        max_neb = max(max_neb, int(m.group(1)))
        except FileNotFoundError:
            pass
        return max_scf, max_opt, max_neb


# ------------------------------------------------------------
# Запуск одного расчёта
# ------------------------------------------------------------

def run_orca_job(inp_file: Path, work_dir: Path) -> str:
    """Запускает один input-файл ORCA. Возвращает статус."""
    base_name = inp_file.stem
    out_file = work_dir / f"{base_name}.out"
    log.info(f"=== Запуск: {inp_file.name} -> {out_file.name} ===")

    # Резервная копия старого лога
    if out_file.exists():
        bak = out_file.with_suffix(f".out.bak_{datetime.now():%Y%m%d_%H%M%S}")
        shutil.move(str(out_file), str(bak))
        log.info(f"Старый лог сохранён как {bak.name}")

    # Открываем .out-файл, в который будем писать stdout ORCA
    # Передаём ТОЛЬКО имя файла (cwd уже = work_dir)
    cmd = [ORCA_EXE, inp_file.name]
    log.info(f"Команда: {' '.join(cmd)} (cwd={work_dir})")

    out_fh = open(out_file, "w", encoding="utf-8", errors="ignore", buffering=1)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(work_dir),
            stdout=out_fh,        # ← stdout ORCA идёт прямо в .out
            stderr=subprocess.STDOUT,  # ← stderr тоже туда же
        )
    except FileNotFoundError:
        out_fh.close()
        log.error(f"Не найден ORCA: {ORCA_EXE}")
        return "error"

    log.info(f"PID ORCA: {proc.pid}")

    monitor = LogMonitor(out_file)
    start_time = time.time()

    try:
        while True:
            time.sleep(CHECK_INTERVAL_SEC)

            retcode = proc.poll()
            if retcode is not None:
                time.sleep(2)
                out_fh.flush()
                new_lines = monitor.read_new_lines()
                status, msg = monitor.analyze(new_lines)
                if status == "success":
                    log.info(f"✔ {inp_file.name} завершён успешно.")
                    return "success"
                diag = [f"returncode={retcode}"]
                if msg:
                    diag.append(msg)
                if out_file.exists():
                    diag.append(f"размер .out={out_file.stat().st_size} байт")
                log.error(f"✘ {inp_file.name} завершён с ошибкой: {' | '.join(diag)}")
                return "error"

            elapsed_h = (time.time() - start_time) / 3600
            if elapsed_h > MAX_RUNTIME_HOURS:
                log.error(f"[KILLED] ✘ Превышен таймаут {MAX_RUNTIME_HOURS} ч")
                kill_process(proc)
                return "timeout"

            if monitor.check_growth():
                log.error(f"[KILLED] ✘ Лог не обновлялся {STALL_TIMEOUT_MIN} мин")
                kill_process(proc)
                return "stuck"

            new_lines = monitor.read_new_lines()
            if new_lines:
                status, msg = monitor.analyze(new_lines)
                if status == "error":
                    log.error(f"[KILLED] ✘ {inp_file.name}: {msg}")
                    kill_process(proc)
                    return "error"
                if status == "stuck":
                    log.error(f"[KILLED] ✘ {inp_file.name}: {msg}")
                    kill_process(proc)
                    return "stuck"

    except KeyboardInterrupt:
        log.warning("[KILLED] Прервано пользователем.")
        kill_process(proc)
        raise
    finally:
        try:
            out_fh.close()
        except Exception:
            pass

# ------------------------------------------------------------
# Основной цикл очереди
# ------------------------------------------------------------

def main():
    work_dir = Path(WORK_DIR) if WORK_DIR else Path(__file__).parent
    log.info(f"Рабочая папка: {work_dir}")
    log.info(f"Всего задач в очереди: {len(QUEUE)}")

    results = {}
    queue_start = time.time()

    for idx, inp_name in enumerate(QUEUE, start=1):
        inp_file = work_dir / inp_name
        task_start = time.time()

        # ---------- Файл не найден ----------
        if not inp_file.exists():
            log.error(f"Файл не найден: {inp_file}. Пропускаю.")
            results[inp_name] = "missing"
            send_vk_message(
                f"ORCA [{idx}/{len(QUEUE)}]: файл не найден",
                f"Файл {inp_file} отсутствует.\n"
                f"Задача {idx} из {len(QUEUE)} пропущена.\n"
                f"Время: {datetime.now():%Y-%m-%d %H:%M:%S}",
            )
            continue

        # ---------- Запуск задачи ----------
        log.info(f"--- Задача {idx}/{len(QUEUE)}: {inp_name} ---")
        status = run_orca_job(inp_file, work_dir)
        results[inp_name] = status

        duration = time.time() - task_start
        duration_str = format_duration(duration)
        out_path = work_dir / f"{inp_file.stem}.out"

        # ---------- Успех ----------
        if status == "success":
            log.info(f"✔ {inp_name} готов за {duration_str}")
            send_vk_message(
                f"ORCA [{idx}/{len(QUEUE)}] ✔ {inp_name}",
                f"Задача {idx} из {len(QUEUE)} завершена УСПЕШНО.\n\n"
                f"Файл:       {inp_name}\n"
                f"Статус:     success\n"
                f"Время:      {duration_str}\n"
                f"Результат:  {out_path.name}\n"
                f"Очередь:    {idx} из {len(QUEUE)} выполнено\n"
                f"Завершено:  {datetime.now():%Y-%m-%d %H:%M:%S}",
            )
            continue

        # ---------- Ошибка / таймаут / зависание ----------
        error_label = {
            "error":   "ОШИБКА",
            "timeout": "ТАЙМАУТ",
            "stuck":   "ЗАВИС",
        }.get(status, status.upper())

        log.error(f"✘ {inp_name}: {error_label} за {duration_str}")
        send_vk_message(
            f"ORCA [{idx}/{len(QUEUE)}] ✘ {inp_name} — {error_label}",
            f"Задача {idx} из {len(QUEUE)} завершилась НЕУДАЧНО.\n\n"
            f"Файл:       {inp_name}\n"
            f"Статус:     {status}\n"
            f"Причина:    {error_label}\n"
            f"Время:      {duration_str}\n"
            f"Лог:        {out_path.name}\n"
            f"Завершено:  {datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
            f"Проверьте .out-файл для деталей.",
        )

    # ---------- Итоговый отчёт ----------
    total_duration = time.time() - queue_start
    log.info("========== ИТОГИ ОЧЕРЕДИ ==========")
    for name, st in results.items():
        log.info(f"{name}: {st}")

    # Готовим красивую сводку
    success_count = sum(1 for s in results.values() if s == "success")
    fail_count = len(results) - success_count

    summary_lines = []
    for name, st in results.items():
        mark = "✔" if st == "success" else "✘"
        summary_lines.append(f"  {mark} {name}: {st}")
    summary = "\n".join(summary_lines)

    overall = "ВСЁ УСПЕШНО" if fail_count == 0 else f"{fail_count} ОШИБОК"

    send_vk_message(
        f"ORCA: очередь завершена — {overall}",
        f"ИТОГОВЫЙ ОТЧЁТ\n\n"
        f"Всего задач:    {len(QUEUE)}\n"
        f"Успешно:        {success_count}\n"
        f"С ошибками:     {fail_count}\n"
        f"Общее время:    {format_duration(total_duration)}\n"
        f"Завершено:      {datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
        f"Детали по задачам:\n{summary}",
    )


def format_duration(seconds: float) -> str:
    """Превращает секунды в строку вида '1ч 23м 45с'."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}ч {m}м {s}с"
    if m > 0:
        return f"{m}м {s}с"
    return f"{s}с"


if __name__ == "__main__":
    main()