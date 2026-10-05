"""
bot_worker.py

Поток-исполнитель расчётов ORCA.
Работает в фоне, берёт задачи из очереди, следит за .out,
отправляет уведомления через переданный callback.
"""

import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from bot_config import (
    ORCA_EXE, WORK_DIR, WATCH_INTERVAL_SEC, CHECK_INTERVAL_SEC,
    STALL_TIMEOUT_MIN, MAX_RUNTIME_HOURS,
    MAX_SCF_ITER_WARN, MAX_OPT_CYCLES_WARN, BACKUP_DIR,
    CLEANUP_ENABLED, DELETE_PATTERNS, DELETE_FOLDERS,
)
from bot_parser import InpFile

WORK_PATH = Path(WORK_DIR)


# ============================================================
# ==================== РЕГЕКСПЫ И ПАТТЕРНЫ ==================
# ============================================================

ERROR_PATTERNS = [
    re.compile(r"ORCA finished by error termination", re.IGNORECASE),
    re.compile(r"aborting the run", re.IGNORECASE),
    re.compile(r"Error \(ORCA\)", re.IGNORECASE),
    re.compile(r"SCF NOT CONVERGED", re.IGNORECASE),
    re.compile(r"Geometry optimization did not converge", re.IGNORECASE),
]
SUCCESS_PATTERNS = [
    re.compile(r"ORCA TERMINATED NORMALLY", re.IGNORECASE),
    re.compile(r"TOTAL RUN TIME", re.IGNORECASE),
]
RE_SCF_ITER  = re.compile(r"^\s{3,}(\d+)\s+(-?\d+\.\d{8,})", re.MULTILINE)
RE_OPT_CYCLE = re.compile(r"GEOMETRY OPTIMIZATION CYCLE\s+(\d+)", re.IGNORECASE)
RE_NEB_ITER  = re.compile(r"NEB\s+ITERATION\s+(\d+)", re.IGNORECASE)


# ============================================================
# ==================== LogMonitor ============================
# ============================================================

class LogMonitor:
    """Следит за .out-файлом ORCA."""

    def __init__(self, out_file: Path):
        self.out_file = out_file
        self.last_size = 0
        self.last_growth_time = time.time()

    def check_growth(self) -> bool:
        try:
            size = self.out_file.stat().st_size
        except FileNotFoundError:
            return False
        if size > self.last_size:
            self.last_size = size
            self.last_growth_time = time.time()
            return False
        return (time.time() - self.last_growth_time) > STALL_TIMEOUT_MIN * 60

    def analyze(self) -> tuple[str, str]:
        if not self.out_file.exists():
            return "ok", ""
        try:
            with open(self.out_file, "r", encoding="utf-8",
                      errors="ignore") as f:
                text = f.read()
        except Exception:
            return "ok", ""

        for p in SUCCESS_PATTERNS:
            if p.search(text):
                return "success", "ORCA завершился нормально"

        for p in ERROR_PATTERNS:
            if p.search(text):
                return "error", f"Обнаружено: {p.pattern}"

        max_scf_block, max_opt, max_neb = self._scan_counters(text)

        if max_scf_block > MAX_SCF_ITER_WARN:
            return "stuck", (
                f"SCF не сходится: {max_scf_block} итераций в одном блоке"
            )
        if max_opt > MAX_OPT_CYCLES_WARN:
            return "stuck", f"Оптимизация не сходится: {max_opt} циклов"
        if max_neb > 500:
            return "stuck", f"NEB не сходится: {max_neb} итераций"

        return "ok", ""

    @staticmethod
    def _scan_counters(text: str) -> tuple[int, int, int]:
        max_scf_block = 0
        current_block = 0
        max_opt = 0
        max_neb = 0

        for line in text.splitlines():
            m = RE_SCF_ITER.search(line)
            if m:
                iter_num = int(m.group(1))
                if iter_num == 1:
                    max_scf_block = max(max_scf_block, current_block)
                    current_block = 1
                else:
                    current_block = max(current_block, iter_num)
                continue

            m = RE_OPT_CYCLE.search(line)
            if m:
                max_opt = max(max_opt, int(m.group(1)))

            m = RE_NEB_ITER.search(line)
            if m:
                max_neb = max(max_neb, int(m.group(1)))

        max_scf_block = max(max_scf_block, current_block)
        return max_scf_block, max_opt, max_neb


# ============================================================
# ==================== ГЕНЕРАЦИЯ ИМЕНИ ======================
# ============================================================

TASK_SUFFIX = {
    "SP":       "SP",
    "EnGrad":   "EnGrad",
    "Opt":      "Opt",
    "OptTS":    "OptTS",
    "Freq":     "Freq",
    "NumFreq":  "NumFreq",
}


def make_job_name(inp_path: Path) -> Path:
    inp = InpFile(inp_path)
    params = inp.get_params()
    task = params.get("task") or "job"
    suffix = TASK_SUFFIX.get(task, task)

    stem = inp_path.stem
    for s in TASK_SUFFIX.values():
        if stem.endswith(f"_{s}"):
            return inp_path

    return inp_path.with_name(f"{stem}_{suffix}.inp")


# ============================================================
# ========================= WORKER ===========================
# ============================================================

class OrcaWorker:
    """Фоновый поток, который выполняет расчёты из очереди."""

    def __init__(self, notify_callback):
        self.notify = notify_callback
        self.job_queue: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._current_proc: subprocess.Popen | None = None
        self._current_task: str | None = None
        self._lock = threading.Lock()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self._stop_flag.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        print("[worker] Поток-исполнитель запущен")

    def stop(self):
        self._stop_flag.set()
        with self._lock:
            if self._current_proc and self._current_proc.poll() is None:
                self._kill(self._current_proc)

    def enqueue(self, inp_path: Path):
        self.job_queue.put(Path(inp_path))
        print(f"[worker] В очередь добавлено: {inp_path.name}")

    def queue_size(self) -> int:
        return self.job_queue.qsize()

    def list_queue(self) -> list[str]:
        items = []
        while True:
            try:
                items.append(self.job_queue.get_nowait())
                self.job_queue.task_done()
            except queue.Empty:
                break
        for item in items:
            self.job_queue.put(item)
        return [p.name for p in items]

    def clear_queue(self) -> int:
        removed = 0
        while True:
            try:
                self.job_queue.get_nowait()
                self.job_queue.task_done()
                removed += 1
            except queue.Empty:
                break
        print(f"[worker] Очередь очищена: удалено {removed}")
        return removed

    def remove_from_queue(self, filename: str) -> int:
        items = []
        while True:
            try:
                items.append(self.job_queue.get_nowait())
                self.job_queue.task_done()
            except queue.Empty:
                break

        removed = 0
        for item in items:
            if item.name == filename:
                removed += 1
            else:
                self.job_queue.put(item)

        print(f"[worker] Из очереди удалено {removed}: {filename}")
        return removed

    def current_task(self) -> str | None:
        return self._current_task

    def is_busy(self) -> bool:
        with self._lock:
            return self._current_proc is not None and \
                   self._current_proc.poll() is None

    def stop_current(self) -> bool:
        with self._lock:
            if self._current_proc and self._current_proc.poll() is None:
                self._kill(self._current_proc)
                return True
        return False

    def _loop(self):
        while not self._stop_flag.is_set():
            try:
                inp_path = self.job_queue.get(timeout=2)
            except queue.Empty:
                continue

            try:
                self._run_job(inp_path)
            except Exception as e:
                self.notify(
                    f"❌ Ошибка обработки {inp_path.name}",
                    f"Внутренняя ошибка worker'а: {e}",
                )

    def _run_job(self, inp_path: Path):
        try:
            job_path = make_job_name(inp_path)
        except Exception as e:
            self.notify(
                f"❌ {inp_path.name} — ошибка чтения",
                f"Не удалось определить тип задачи: {e}",
            )
            return

        if job_path != inp_path:
            shutil.copy2(inp_path, job_path)
            print(f"[worker] Копия: {inp_path.name} → {job_path.name}")

        try:
            params = InpFile(job_path).get_params()
        except Exception as e:
            self.notify(f"❌ {job_path.name}", f"Ошибка парсинга: {e}")
            return

        task = params.get("task") or "?"
        theory = params.get("theory") or "?"
        basis = params.get("basis") or "—"

        self.notify(
            f"🚀 Старт: {job_path.name}",
            f"Задача:  {task}\n"
            f"Метод:   {theory}\n"
            f"Базис:   {basis}\n"
            f"Начало:  {datetime.now():%H:%M:%S}",
        )

        out_path = job_path.with_suffix(".out")
        if out_path.exists():
            if BACKUP_DIR:
                Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
                bak = Path(BACKUP_DIR) / \
                      f"{out_path.stem}_{datetime.now():%Y%m%d_%H%M%S}.out"
                shutil.move(str(out_path), str(bak))
            else:
                out_path.unlink()

        try:
            out_fh = open(out_path, "w", encoding="utf-8",
                          errors="ignore", buffering=1)
            proc = subprocess.Popen(
                [ORCA_EXE, job_path.name],
                cwd=str(job_path.parent),
                stdout=out_fh,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            self.notify(f"❌ {job_path.name}",
                        f"Не удалось запустить ORCA: {e}")
            return

        with self._lock:
            self._current_proc = proc
            self._current_task = job_path.name

        print(f"[worker] ORCA PID={proc.pid}, лог={out_path.name}")

        monitor = LogMonitor(out_path)
        start_time = time.time()
        status = "unknown"
        message = ""

        try:
            while True:
                time.sleep(CHECK_INTERVAL_SEC)

                retcode = proc.poll()
                if retcode is not None:
                    time.sleep(2)
                    out_fh.flush()
                    status, message = monitor.analyze()
                    if status == "success":
                        break
                    status = "error"
                    message = f"ORCA завершился с кодом {retcode}"
                    break

                elapsed_h = (time.time() - start_time) / 3600
                if elapsed_h > MAX_RUNTIME_HOURS:
                    status = "timeout"
                    message = f"Превышен лимит {MAX_RUNTIME_HOURS} ч"
                    self._kill(proc)
                    break

                if monitor.check_growth():
                    status = "stuck"
                    message = f"Лог не обновлялся {STALL_TIMEOUT_MIN} мин"
                    self._kill(proc)
                    break

                status_check, msg_check = monitor.analyze()
                if status_check in ("error", "stuck"):
                    status = status_check
                    message = msg_check
                    self._kill(proc)
                    break

        except KeyboardInterrupt:
            self._kill(proc)
            status = "stopped"
            message = "Прервано пользователем"
        finally:
            try:
                out_fh.close()
            except Exception:
                pass
            with self._lock:
                self._current_proc = None
                self._current_task = None

        duration = time.time() - start_time
        dur_str = self._format_duration(duration)

        if status == "success":
            self.notify(
                f"✅ Готово: {job_path.name}",
                f"Время: {dur_str}\nФайл:  {out_path.name}",
            )
        elif status == "stopped":
            self.notify(
                f"⏹ Остановлено: {job_path.name}",
                f"Время: {dur_str}\nПричина: {message}",
            )
        else:
            label = {
                "error":   "ОШИБКА",
                "timeout": "ТАЙМАУТ",
                "stuck":   "ЗАВИС",
            }.get(status, status.upper())
            self.notify(
                f"❌ {label}: {job_path.name}",
                f"Время:   {dur_str}\n"
                f"Причина: {message}\n"
                f"Лог:     {out_path.name}",
            )

        self._cleanup_job(job_path.stem)

    def _cleanup_job(self, job_stem: str):
        if not CLEANUP_ENABLED:
            return

        deleted = 0

        for pattern in DELETE_PATTERNS:
            for f in WORK_PATH.glob(f"{job_stem}{pattern}"):
                if not f.is_file():
                    continue
                if f.suffix in (".inp", ".out"):
                    continue
                try:
                    f.unlink()
                    deleted += 1
                except Exception as e:
                    print(f"[worker] Не удалить {f.name}: {e}")

        for suffix in DELETE_FOLDERS:
            folder = WORK_PATH / f"{job_stem}{suffix}"
            if folder.exists() and folder.is_dir():
                try:
                    shutil.rmtree(folder)
                    deleted += 1
                except Exception as e:
                    print(f"[worker] Не удалить папку {folder.name}: {e}")

        if deleted:
            print(f"[worker] Очистка {job_stem}: удалено {deleted}")

    @staticmethod
    def _kill(proc: subprocess.Popen):
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
        except Exception as e:
            print(f"[worker] Ошибка убийства процесса: {e}")

    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            return f"{h}ч {m}м {s}с"
        if m > 0:
            return f"{m}м {s}с"
        return f"{s}с"