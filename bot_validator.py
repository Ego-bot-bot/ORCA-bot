"""
bot_validator.py

Проверка совместимости параметров .inp.
Принимает словарь параметров, возвращает список сообщений.

Используется ботом перед /run и в командах /set, чтобы
предупредить о спорных комбинациях.
"""

from bot_vocab import THEORIES, BASES


# ============================================================
# ======================= КОНСТАНТЫ ==========================
# ============================================================

# Композитные методы (содержат базис и dispersion)
COMPOSITE_METHODS = {
    "r2SCAN-3c", "PBE-3c", "B97-3c", "wB97X-3c",
}

# Волновые методы (post-HF)
WAVE_METHODS = {
    "RI-MP2", "DLPNO-MP2", "SCS-DLPNO-MP2",
    "RI-CCSD(T)", "DLPNO-CCSD(T)", "DLPNO-CCSD(T1)",
}

# Локальные методы (для них Freq → NumFreq)
LOCAL_METHODS = {
    "DLPNO-MP2", "SCS-DLPNO-MP2",
    "DLPNO-CCSD(T)", "DLPNO-CCSD(T1)",
}

# Двойные гибриды (очень дорогие)
DOUBLE_HYBRIDS = {"B2GP-PLYP"}

# Функционалы со встроенной нелокальной корреляцией (VV10)
VV10_METHODS = {"B97M-V", "wB97X-V", "wB97M-V"}

# Большие базисы (дорогие)
LARGE_BASES = {
    "def2-QZVP", "def2-QZVPP", "def2-QZVPPD",
    "cc-pVQZ", "aug-cc-pVQZ",
    "pc-3", "aug-pc-3",
}

# Базисы с диффузными функциями (для анионов)
DIFFUSE_BASES = {
    "def2-SVPD", "def2-TZVPPD", "def2-QZVPPD",
    "aug-cc-pVDZ", "aug-cc-pVTZ", "aug-cc-pVQZ",
    "aug-pc-1", "aug-pc-2", "aug-pc-3",
}


# ============================================================
# ======================= ВАЛИДАТОР ==========================
# ============================================================

class Validator:
    """Проверяет комбинацию параметров .inp."""

    def __init__(self):
        self.messages: list[dict] = []

    # --------------------------------------------------------
    # Публичный метод
    # --------------------------------------------------------

    def validate(self, params: dict) -> list[dict]:
        """
        Принимает словарь параметров, возвращает список сообщений.
        Каждое сообщение: {"level": "ok|info|warning|error",
                           "title": "...",
                           "text": "..."}
        """
        self.messages = []

        task   = params.get("task")
        theory = params.get("theory")
        basis  = params.get("basis")
        disp   = params.get("dispersion")
        solvent = params.get("solvent")
        model  = params.get("model")
        charge = params.get("charge")
        mult   = params.get("multiplicity")

        # --- Проверки ---
        self._check_basic_presence(task, theory)
        self._check_composite(theory, basis, disp)
        self._check_wave_with_opt(task, theory)
        self._check_local_with_freq(task, theory)
        self._check_double_hybrid(theory, task, basis)
        self._check_hf_dispersion(theory, disp)
        self._check_vv10_with_disp(theory, disp)
        self._check_charge_mult(charge, mult)
        self._check_anion_basis(charge, basis)
        self._check_opt_freq_sequence(task, theory)
        self._check_solvent_model(solvent, model)

        # --- Если нет проблем — говорим "ok" ---
        if not self.messages:
            self.messages.append({
                "level": "ok",
                "title": "Совместимость в порядке",
                "text": "Параметры согласованы. Можно запускать.",
            })

        return self.messages

    # --------------------------------------------------------
    # Внутренние проверки
    # --------------------------------------------------------

    def _msg(self, level: str, title: str, text: str):
        self.messages.append({
            "level": level,
            "title": title,
            "text": text,
        })

    def _check_basic_presence(self, task: str | None, theory: str | None):
        """Проверяет, что тип задачи и метод заданы."""
        if not task:
            self._msg(
                "error",
                "Не задан тип задачи",
                "Укажите задачу: /set <file> task Opt "
                "(варианты: SP, EnGrad, Opt, OptTS, Freq, NumFreq).",
            )
        if not theory:
            self._msg(
                "error",
                "Не задан метод",
                "Укажите метод: /set <file> theory B3LYP "
                "(список: /methods).",
            )

    def _check_composite(self, theory, basis, disp):
        """Композитные методы уже содержат базис и dispersion."""
        if theory not in COMPOSITE_METHODS:
            return

        if basis:
            self._msg(
                "warning",
                f"{theory} — композитный метод",
                f"Он уже содержит базис (обычно def2-mTZVPP). "
                f"Ваш базис {basis} будет проигнорирован ORCA.\n"
                f"Советую убрать базис или выбрать "
                f"полноценный метод: /methods",
            )
        if disp:
            self._msg(
                "warning",
                f"{theory} — композитный метод",
                f"Он уже содержит dispersion (обычно D4). "
                f"Ваш {disp} будет проигнорирован.\n"
                f"Советую убрать dispersion.",
            )

    def _check_wave_with_opt(self, task, theory):
        """Волновые методы + Opt/OptTS — очень дорого."""
        if theory not in WAVE_METHODS:
            return

        if task == "Opt":
            self._msg(
                "warning",
                f"{theory} + Opt — очень дорого",
                "Оптимизация геометрии на волновом методе "
                "требует аналитических градиентов и стоит недели CPU.\n"
                "Рекомендуемый workflow:\n"
                "  1) Opt с DFT (например, r2SCAN-3c) — быстро.\n"
                "  2) SP с " + theory + " — одна точка, точно.\n"
                "  3) Freq с DFT для термохимии.",
            )
        elif task == "OptTS":
            self._msg(
                "error",
                f"{theory} + OptTS — не рекомендуется",
                "Поиск TS на волновом методе крайне медленный "
                "и часто не сходится.\n"
                "Сделайте OptTS на DFT, затем SP с " + theory + ".",
            )

    def _check_local_with_freq(self, task, theory):
        """DLPNO + аналитический Freq невозможен."""
        if theory in LOCAL_METHODS and task == "Freq":
            self._msg(
                "error",
                f"{theory} + Freq — невозможно",
                "Для DLPNO-методов аналитический Freq не реализован.\n"
                "Используйте: /set <file> task NumFreq "
                "(численный частотный анализ).",
            )

    def _check_double_hybrid(self, theory, task, basis):
        """B2GP-PLYP — очень дорого."""
        if theory not in DOUBLE_HYBRIDS:
            return

        if task in ("Opt", "OptTS", "Freq"):
            self._msg(
                "warning",
                f"{theory} + {task} — очень дорого",
                "Двойные гибриды требуют MP2-подобного шага и "
                "работают медленно.\n"
                "Советую: Opt/OptTS/Freq на DFT, "
                f"а {theory} использовать только для SP.",
            )
        if basis in LARGE_BASES:
            self._msg(
                "warning",
                f"{theory} + {basis}",
                "Двойной гибрид с большим базисом — "
                "часы CPU даже для малых молекул.",
            )

    def _check_hf_dispersion(self, theory, disp):
        """HF не учитывает корреляцию — dispersion полезен."""
        if theory == "HF" and not disp:
            self._msg(
                "info",
                "HF без dispersion",
                "HF не учитывает дисперсионные взаимодействия. "
                "Если считаете слабые взаимодействия — добавьте D4:\n"
                "/set <file> dispersion D4",
            )

    def _check_vv10_with_disp(self, theory, disp):
        """V-методы уже содержат нелокальную корреляцию."""
        if theory in VV10_METHODS and disp in ("D3BJ", "D3ZERO", "D4"):
            self._msg(
                "warning",
                f"{theory} + {disp} — двойной учёт",
                "Этот метод уже содержит нелокальную корреляцию VV10. "
                "Дополнительный dispersion D3/D4 избыточен.\n"
                "Советую убрать dispersion: "
                "/set <file> dispersion -",
            )

    def _check_charge_mult(self, charge, mult):
        """Проверка базовых ограничений заряда/мультиплетности."""
        if charge is None or mult is None:
            return

        if mult < 1:
            self._msg(
                "error",
                "Мультиплетность < 1",
                "Мультиплетность должна быть ≥ 1.",
            )
            return

        # Простое правило чётности: charge + mult
        # Если заряд чётный (0, ±2), то электронов чётное,
        # и мультиплетность должна быть нечётной (1, 3, 5...).
        # Если заряд нечётный (±1, ±3), то мультиплетность чётная (2, 4...).
        parity_charge = abs(charge) % 2
        parity_mult = (mult - 1) % 2

        if parity_charge != parity_mult:
            self._msg(
                "warning",
                "Возможное несоответствие charge ↔ mult",
                f"Заряд {charge} и мультиплетность {mult} могут "
                f"противоречить друг другу.\n"
                f"Правило (для нейтральных молекул):\n"
                f"  чётный заряд → нечётная мультиплетность (1, 3, 5)\n"
                f"  нечётный заряд → чётная мультиплетность (2, 4)\n"
                f"Проверьте: правильно ли указан заряд?",
            )

    def _check_anion_basis(self, charge, basis):
        """Для анионов желателен диффузный базис."""
        if charge is None or charge >= 0 or not basis:
            return
        if basis in DIFFUSE_BASES:
            return
        self._msg(
            "info",
            "Анион без диффузных функций",
            f"Заряд {charge}, базис {basis} без диффузных функций.\n"
            f"Для анионов это может дать ошибку в энергии. "
            f"Советую базис с диффузией: aug-cc-pVTZ, def2-TZVPPD, "
            f"aug-pc-2 и т.п.",
        )

    def _check_opt_freq_sequence(self, task, theory):
        """Совет про последовательность Opt → Freq."""
        if task == "Opt":
            self._msg(
                "info",
                "После Opt — проверьте частоты",
                "Рекомендую после оптимизации запустить Freq: "
                "проверить, что все частоты положительные "
                "(это минимум, а не TS).\n"
                "Команда: /prepare <file> task Freq",
            )
        elif task == "OptTS":
            self._msg(
                "info",
                "Проверка TS",
                "После OptTS обязательно запустите Freq "
                "(или NumFreq). У настоящего TS должна быть "
                "ровно ОДНА мнимая частота.\n"
                "Плюс IRC — чтобы убедиться, что TS ведёт "
                "к правильным реагентам/продуктам.",
            )
        elif task == "NumFreq":
            self._msg(
                "info",
                "NumFreq — очень медленно",
                "Численный частотный анализ требует "
                "2 × 3N одиночных расчётов. "
                "Для систем > 30 атомов может занять часы.",
            )

    def _check_solvent_model(self, solvent, model):
        """Проверки совместимости растворителя и модели."""
        if solvent and not model:
            self._msg(
                "warning",
                "Растворитель без модели",
                "Указан растворитель, но не выбрана модель сольватации.\n"
                "По умолчанию применится SMD. "
                "Можно задать: /set <file> model SMD",
            )
        if model in ("SMD", "CPCM") and not solvent:
            self._msg(
                "error",
                f"Модель {model} без растворителя",
                f"Модель {model} требует указания растворителя.\n"
                f"Задайте: /set <file> solvent benzene",
            )


# ============================================================
# ==================== УДОБНАЯ ОБЁРТКА =======================
# ============================================================

def validate_params(params: dict) -> list[dict]:
    """Простой вызов: validate_params(params) → список сообщений."""
    return Validator().validate(params)


def format_messages(messages: list[dict]) -> str:
    """
    Форматирует список сообщений в текст для бота.
    Возвращает готовый блок для отправки в VK.
    """
    icons = {
        "ok":      "✅",
        "info":    "ℹ️",
        "warning": "⚠️",
        "error":   "❌",
    }

    if not messages:
        return "Сообщений нет."

    lines = []
    for m in messages:
        icon = icons.get(m["level"], "•")
        lines.append(f"{icon} **{m['title']}**")
        lines.append(m["text"])
        lines.append("")   # пустая строка между блоками

    return "\n".join(lines).rstrip()