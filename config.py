"""Минимальный загрузчик .env без внешних зависимостей."""
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    out: dict[str, str] = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


def get_int(env: dict[str, str], key: str, default: int) -> int:
    try:
        return int(env.get(key, default))
    except (ValueError, TypeError):
        return default


def get_bool(env: dict[str, str], key: str, default: bool) -> bool:
    val = env.get(key, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def get_paths(env: dict[str, str], key: str, fallback: list = None) -> list:
    """Список путей из переменной с разделителем ';'.

    Пустые элементы и пробелы убираются. Возвращает [] если ключа нет и
    fallback не задан.
    """
    from pathlib import Path
    raw = env.get(key, "")
    if not raw:
        return list(fallback or [])
    out: list = []
    for item in raw.split(";"):
        item = item.strip().strip('"').strip("'")
        if item:
            out.append(Path(item))
    return out or list(fallback or [])
