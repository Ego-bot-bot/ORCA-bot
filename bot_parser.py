"""
bot_parser.py

Чтение, разбор и модификация .inp-файлов ORCA.

Умеет:
- извлекать текущие параметры (метод, базис, задача, charge, mult, ...)
- менять их, не трогая:
  * координаты атомов (блок * xyz ... *)
  * блоки %pal и %maxcore
  * прочие строки, которые мы не распознаём
"""

import re
import logging
from pathlib import Path

log = logging.getLogger(__name__)


# ============================================================
# ===================== СПИСКИ ТОКЕНОВ =======================
# ============================================================

# Типы задач (в строке !)
TASK_KEYWORDS = {
    "SP", "EnGrad", "Opt", "OptTS", "Freq", "NumFreq",
}

# Теории (методы)
THEORY_KEYWORDS = {
    "r2SCAN-3c", "PBE-3c", "B97-3c", "wB97X-3c",
    "LDA", "PBE", "B97M-V", "B97M-D4", "r2SCAN",
    "PBE0", "r2SCAN0", "wB97X-D4", "wB97X-V",
    "wB97M-D4", "wB97M-V", "Pr2SCAN69",
    "B2GP-PLYP", "HF",
    "RI-MP2", "DLPNO-MP2", "SCS-DLPNO-MP2",
    "RI-CCSD(T)", "DLPNO-CCSD(T)", "DLPNO-CCSD(T1)",
}

# Базисы
BASIS_KEYWORDS = {
    "def2-SVP", "def2-TZVP", "def2-QZVP",
    "def2-TZVPP", "def2-QZVPP",
    "def2-SVPD", "def2-TZVPPD", "def2-QZVPPD",
    "cc-pVDZ", "cc-pVTZ", "cc-pVQZ",
    "aug-cc-pVDZ", "aug-cc-pVTZ", "aug-cc-pVQZ",
    "pc-1", "pc-2", "pc-3",
    "aug-pc-1", "aug-pc-2", "aug-pc-3",
}

# Dispersion
DISPERSION_KEYWORDS = {
    "D3BJ", "D3ZERO", "D4", "NL", "SCNL",
}

# Модели сольватации (без растворителя, отдельным токеном)
MODEL_KEYWORDS = {
    "CPCM", "COSMO_RS", "ALPB", "ddCOSMO", "CPCMX",
}
# SMD обрабатывается отдельно — потому что пишется как SMD(растворитель)

# Дополнительные часто встречающиеся ключи (не трогаем)
IGNORE_KEYWORDS = {
    "TightSCF", "SlowConv", "VerySlowConv", "TRAH",
    "Grid4", "Grid5", "Grid6", "NoFinalGrid",
    "RIJCOSX", "RI", "NORI",
    "PAL", "NOSOSCF", "SOSCF",
    "XYZFile", "MOREAD", "MOInp",
    "KeepDens", "KeepInts", "KeepFiles",
    "NormalSCF", "Direct", "NoIter",
    "AnFreq", "NumGrad",
}


# ============================================================
# ==================== КЛАСС .inp-ФАЙЛА =====================
# ============================================================

class InpFile:
    """Представление одного .inp-файла ORCA."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lines: list[str] = []
        self._load()

    # --------------------------------------------------------
    # Загрузка / сохранение
    # --------------------------------------------------------

    def _load(self):
        if not self.path.exists():
            raise FileNotFoundError(f"Файл не найден: {self.path}")
        with open(self.path, "r", encoding="utf-8") as f:
            self.lines = f.readlines()

    def save(self, backup: bool = True):
        """Сохраняет файл. По умолчанию делает .bak."""
        if backup:
            bak = self.path.with_suffix(".inp.bak")
            try:
                with open(bak, "w", encoding="utf-8") as f:
                    f.writelines(self.lines)
            except Exception as e:
                log.warning(f"Не удалось создать .bak: {e}")

        with open(self.path, "w", encoding="utf-8") as f:
            f.writelines(self.lines)
        log.info(f"Сохранён {self.path.name}")

    # --------------------------------------------------------
    # Поиск строк
    # --------------------------------------------------------

    def _find_simple_line(self) -> int | None:
        """Возвращает индекс строки, начинающейся с '!'."""
        for i, line in enumerate(self.lines):
            if line.lstrip().startswith("!"):
                return i
        return None

    def _find_xyz_block(self) -> tuple[int, int] | None:
        """
        Возвращает (индекс_первой, индекс_последней) строки блока * xyz.
        Ищет строку '* xyz C M' и соответствующую закрывающую '*'.
        """
        start = None
        for i, line in enumerate(self.lines):
            stripped = line.strip()
            if stripped.startswith("*") and "xyz" in stripped:
                start = i
                break
        if start is None:
            return None

        # Ищем закрывающую строку '*'
        for j in range(start + 1, len(self.lines)):
            if self.lines[j].strip() == "*":
                return start, j
        return None

    # --------------------------------------------------------
    # Разбор строки '!'
    # --------------------------------------------------------

    def get_simple_tokens(self) -> list[str]:
        """
        Возвращает список токенов из строки '!'.
        Например: ['B3LYP', 'def2-TZVP', 'OPT', 'D4', 'SMD(benzene)']
        """
        idx = self._find_simple_line()
        if idx is None:
            return []
        line = self.lines[idx].lstrip().lstrip("!").strip()
        return line.split()

    def get_params(self) -> dict:
        """Возвращает словарь текущих параметров .inp."""
        tokens = self.get_simple_tokens()
        params = {
            "task": None,
            "theory": None,
            "basis": None,
            "dispersion": None,
            "solvent": None,
            "model": None,
            "extra": [],
            "charge": None,
            "multiplicity": None,
        }

        for tok in tokens:
            # SMD(solvent) — модель + растворитель
            m = re.match(r"^(SMD|CPCM)\(([^)]+)\)$", tok)
            if m:
                params["model"] = m.group(1)
                params["solvent"] = m.group(2).strip()
                continue

            if tok in TASK_KEYWORDS:
                params["task"] = tok
            elif tok in THEORY_KEYWORDS:
                params["theory"] = tok
            elif tok in BASIS_KEYWORDS:
                params["basis"] = tok
            elif tok in DISPERSION_KEYWORDS:
                params["dispersion"] = tok
            elif tok in MODEL_KEYWORDS:
                params["model"] = tok
            else:
                params["extra"].append(tok)

        # Заряд и мультиплетность — из строки * xyz C M
        m_xyz = self._parse_xyz_header()
        if m_xyz is not None:
            params["charge"], params["multiplicity"] = m_xyz

        return params

    def _parse_xyz_header(self) -> tuple[int, int] | None:
        """Ищет строку '* xyz C M'. Возвращает (charge, multiplicity)."""
        for line in self.lines:
            m = re.match(r"^\s*\*\s*xyz\s+(-?\d+)\s+(\d+)",
                         line)
            if m:
                return int(m.group(1)), int(m.group(2))
        return None

    # --------------------------------------------------------
    # Изменение параметров
    # --------------------------------------------------------

    def set_task(self, task: str):
        """Устанавливает тип задачи (удаляет старый)."""
        if task not in TASK_KEYWORDS:
            raise ValueError(f"Неизвестный тип задачи: {task}")
        self._replace_token_of_category(TASK_KEYWORDS, task)

    def set_theory(self, theory: str):
        if theory not in THEORY_KEYWORDS:
            raise ValueError(f"Неизвестный метод: {theory}")
        self._replace_token_of_category(THEORY_KEYWORDS, theory)

    def set_basis(self, basis: str):
        if basis not in BASIS_KEYWORDS:
            raise ValueError(f"Неизвестный базис: {basis}")
        self._replace_token_of_category(BASIS_KEYWORDS, basis)

    def set_dispersion(self, disp: str):
        if disp and disp not in DISPERSION_KEYWORDS:
            raise ValueError(f"Неизвестный dispersion: {disp}")
        self._replace_token_of_category(DISPERSION_KEYWORDS,
                                        disp if disp else None)

    def set_solvent(self, solvent: str):
        """Устанавливает растворитель. Модель берётся из текущей или SMD."""
        params = self.get_params()
        model = params.get("model") or "SMD"
        if model not in ("SMD", "CPCM"):
            # Для других моделей — без аргумента
            self._replace_token_of_category(MODEL_KEYWORDS, model)
            return
        token = f"{model}({solvent})"
        # Ищем и удаляем старое SMD(...) или CPCM(...)
        self._replace_solvent_token(token)

    def set_model(self, model: str):
        """Устанавливает модель сольватации (без растворителя)."""
        if model in ("SMD", "CPCM"):
            # Нужен растворитель — оставляем текущий
            params = self.get_params()
            solv = params.get("solvent") or "water"
            self._replace_solvent_token(f"{model}({solv})")
        elif model in MODEL_KEYWORDS:
            # Сначала удаляем SMD(...)/CPCM(...)
            self._replace_solvent_token(None)
            self._replace_token_of_category(MODEL_KEYWORDS, model)
        else:
            raise ValueError(f"Неизвестная модель: {model}")

    def set_charge(self, charge: int):
        self._replace_xyz_header(charge, None)

    def set_multiplicity(self, mult: int):
        if mult < 1:
            raise ValueError("Мультиплетность должна быть >= 1")
        self._replace_xyz_header(None, mult)

    # --------------------------------------------------------
    # Внутренние помощники
    # --------------------------------------------------------

    def _replace_token_of_category(self, category: set[str],
                                   new_token: str | None):
        """Удаляет все токены из category, добавляет new_token (если не None)."""
        idx = self._find_simple_line()
        if idx is None:
            raise RuntimeError("Строка '!' не найдена в .inp")

        line = self.lines[idx].rstrip("\n")
        # Извлекаем префикс ('!' + пробелы) и токены
        m = re.match(r"^(\s*!\s*)(.*)$", line)
        prefix = m.group(1)
        tokens = m.group(2).split()

        # Удаляем старые
        tokens = [t for t in tokens if t not in category]
        # Добавляем новый (если задан)
        if new_token:
            tokens.append(new_token)

        new_line = prefix + " ".join(tokens) + "\n"
        self.lines[idx] = new_line

    def _replace_solvent_token(self, new_token: str | None):
        """Удаляет SMD(...)/CPCM(...), добавляет new_token (если задан)."""
        idx = self._find_simple_line()
        if idx is None:
            raise RuntimeError("Строка '!' не найдена")

        line = self.lines[idx].rstrip("\n")
        m = re.match(r"^(\s*!\s*)(.*)$", line)
        prefix = m.group(1)
        tokens = m.group(2).split()

        # Удаляем SMD(...)/CPCM(...)
        tokens = [
            t for t in tokens
            if not re.match(r"^(SMD|CPCM)\([^)]+\)$", t)
        ]
        if new_token:
            tokens.append(new_token)

        new_line = prefix + " ".join(tokens) + "\n"
        self.lines[idx] = new_line

    def _replace_xyz_header(self, charge: int | None,
                            mult: int | None):
        """Меняет заряд/мультиплетность в строке '* xyz C M'."""
        for i, line in enumerate(self.lines):
            m = re.match(
                r"^(\s*\*\s*xyz\s+)(-?\d+)(\s+)(\d+)(.*)$", line
            )
            if not m:
                continue
            old_charge = int(m.group(2))
            old_mult = int(m.group(4))

            new_c = charge if charge is not None else old_charge
            new_m = mult if mult is not None else old_mult

            self.lines[i] = (
                f"{m.group(1)}{new_c}{m.group(3)}{new_m}{m.group(5)}\n"
            )
            return
        raise RuntimeError("Строка '* xyz C M' не найдена в .inp")


# ============================================================
# ==================== УДОБНЫЕ ОБЁРТКИ =======================
# ============================================================

def parse_inp(path: Path) -> dict:
    """Прочитать .inp и вернуть параметры."""
    return InpFile(path).get_params()


def update_inp(path: Path, **kwargs) -> InpFile:
    """
    Обновить параметры .inp.

    Пример:
        update_inp("mol1.inp", task="Opt", theory="B3LYP",
                   basis="def2-TZVP", charge=0, multiplicity=1)
    """
    inp = InpFile(path)

    if "task" in kwargs and kwargs["task"]:
        inp.set_task(kwargs["task"])
    if "theory" in kwargs and kwargs["theory"]:
        inp.set_theory(kwargs["theory"])
    if "basis" in kwargs and kwargs["basis"]:
        inp.set_basis(kwargs["basis"])
    if "dispersion" in kwargs:
        inp.set_dispersion(kwargs["dispersion"] or "")
    if "solvent" in kwargs and kwargs["solvent"]:
        inp.set_solvent(kwargs["solvent"])
    if "model" in kwargs and kwargs["model"]:
        inp.set_model(kwargs["model"])
    if "charge" in kwargs and kwargs["charge"] is not None:
        inp.set_charge(int(kwargs["charge"]))
    if "multiplicity" in kwargs and kwargs["multiplicity"] is not None:
        inp.set_multiplicity(int(kwargs["multiplicity"]))

    inp.save()
    return inp