import os
import sys
import time
import base64
import requests
from threading import Thread

try:
    import yaml
except ImportError:
    yaml = None

# --- ANSI цвета ---
RESET   = "\033[0m"
BOLD    = "\033[1m"
CYAN    = "\033[96m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
MAGENTA = "\033[95m"
BLUE    = "\033[94m"

# ─────────────────────────────────────────────
# Настройки
# ─────────────────────────────────────────────
SOURCES_FILE     = "sources.yaml"
OUTPUT_DIR       = "configs"
COMBINED_FILE    = os.path.join(OUTPUT_DIR, "all_configs.txt")
REQUEST_TIMEOUT  = 12  # секунды

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ConfigCollector/1.0)"
}

# Префикс протокола -> имя выходного файла.
# Несколько префиксов могут указывать на один и тот же файл (алиасы).
PROTOCOL_PREFIXES = [
    ("vmess://", "vmess"),
    ("vless://", "vless"),
    ("trojan://", "trojan"),
    ("ss://", "ss"),
    ("ssr://", "ssr"),
    ("hysteria2://", "hysteria2"),
    ("hy2://", "hysteria2"),
    ("tuic://", "tuic"),
]


# ─────────────────────────────────────────────
# Утилиты
# ─────────────────────────────────────────────

def is_ci() -> bool:
    """Возвращает True, если скрипт запущен в CI (GitHub Actions)."""
    return os.getenv("CI") == "true" or os.getenv("GITHUB_ACTIONS") == "true"


def animate_loading(stop_event: dict):
    """Спиннер — показывается только в интерактивном терминале."""
    chars = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    i = 0
    while not stop_event["done"]:
        sys.stdout.write(f"\r{CYAN}[ {chars[i % len(chars)]} Сбор... ]{RESET} ")
        sys.stdout.flush()
        time.sleep(0.08)
        i += 1


def shorten_url(url: str) -> str:
    parts = url.split("/")
    if len(parts) > 4:
        return f"{parts[3]}/.../{parts[-1]}"
    return url[-40:]


def load_urls(path: str = SOURCES_FILE) -> list[str]:
    """Загружает список источников из YAML-файла."""
    if yaml is None:
        print(f"{RED}PyYAML не установлен. Установите: pip install pyyaml{RESET}")
        sys.exit(1)

    if not os.path.exists(path):
        print(f"{RED}Файл источников '{path}' не найден.{RESET}")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    urls = data.get("urls", []) if isinstance(data, dict) else []
    urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]

    if not urls:
        print(f"{RED}В '{path}' не найдено ни одного URL.{RESET}")
        sys.exit(1)

    return urls


def get_mirror_urls(original_url: str) -> list[str]:
    """Прямая ссылка + JSDelivr CDN-зеркала для raw.githubusercontent.com."""
    urls = [original_url]

    if "raw.githubusercontent.com" in original_url:
        p = original_url.split("/")
        if len(p) >= 7:
            user, repo, branch = p[3], p[4], p[5]
            file_path = "/".join(p[6:])
            urls.append(f"https://cdn.jsdelivr.net/gh/{user}/{repo}@{branch}/{file_path}")
            urls.append(f"https://fastly.jsdelivr.net/gh/{user}/{repo}@{branch}/{file_path}")

    elif "github.com" in original_url and "/raw/" in original_url:
        p = original_url.split("/")
        if len(p) >= 8:
            user, repo, branch = p[3], p[4], p[6]
            file_path = "/".join(p[7:])
            urls.append(f"https://cdn.jsdelivr.net/gh/{user}/{repo}@{branch}/{file_path}")
            urls.append(f"https://fastly.jsdelivr.net/gh/{user}/{repo}@{branch}/{file_path}")

    return urls


def get_protocol(line: str) -> str | None:
    """Определяет протокол строки по префиксу. Возвращает None, если протокол не поддерживается."""
    lowered = line.lower()
    for prefix, name in PROTOCOL_PREFIXES:
        if lowered.startswith(prefix):
            return name
    return None


def extract_valid_lines(text: str) -> list[tuple[str, str]]:
    """
    Разбирает текст на строки и оставляет только те, что относятся
    к поддерживаемым протоколам. Возвращает список (протокол, строка).
    Это и есть фильтрация по протоколам — отсекает HTML-мусор, комментарии,
    случайные http(s):// ссылки и т.п.
    """
    result = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        protocol = get_protocol(line)
        if protocol:
            result.append((protocol, line))
    return result


def decode_content(raw: str) -> tuple[list[tuple[str, str]], str]:
    """
    Определяет тип контента и возвращает (список (протокол, строка), тип).
    Типы: 'Plain', 'Base64', 'Empty', 'B64_Err'
    """
    content = raw.strip()
    if not content:
        return [], "Empty"

    # Если в тексте есть хотя бы один протокол-префикс — это Plain-текст
    if "://" in content:
        lines = extract_valid_lines(content)
        return lines, "Plain"

    # Иначе пробуем Base64
    try:
        padding = len(content) % 4
        if padding:
            content += "=" * (4 - padding)
        decoded = base64.b64decode(content).decode("utf-8", errors="ignore")
        lines = extract_valid_lines(decoded)
        return lines, "Base64"
    except Exception:
        return [], "B64_Err"


def fetch_url(url: str) -> str | None:
    """Загружает URL (с зеркалами), возвращает текст или None при ошибке."""
    for mirror in get_mirror_urls(url):
        try:
            r = requests.get(mirror, timeout=REQUEST_TIMEOUT, headers=HEADERS)
            r.raise_for_status()
            return r.text
        except requests.RequestException:
            continue
    return None


def parse_configs(url: str) -> tuple[list[tuple[str, str]], str]:
    """Загружает и декодирует конфиги из одного URL."""
    in_ci = is_ci()

    stop_event = {"done": False}
    spinner = None

    if not in_ci:
        spinner = Thread(target=animate_loading, args=(stop_event,), daemon=True)
        spinner.start()

    try:
        raw = fetch_url(url)

        stop_event["done"] = True
        if not in_ci:
            sys.stdout.write("\r" + " " * 30 + "\r")

        if raw is None:
            return [], "Net_Err"

        return decode_content(raw)

    except KeyboardInterrupt:
        stop_event["done"] = True
        if not in_ci:
            sys.stdout.write("\r" + " " * 30 + "\r")
        raise
    except Exception:
        stop_event["done"] = True
        if not in_ci:
            sys.stdout.write("\r" + " " * 30 + "\r")
        return [], "Error"


def dedup_key(line: str) -> str:
    """
    Ключ для дедупликации: та же нода может встречаться в разных списках
    с разным #remark (именем) в конце — убираем его перед сравнением.
    """
    return line.split("#", 1)[0].strip()


def write_atomic(path: str, content: str) -> bool:
    """Записывает файл атомарно (через .tmp + os.replace), чтобы не оставить битый файл при сбое."""
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
        return True
    except OSError as e:
        print(f"{RED}Ошибка записи '{path}': {e}{RESET}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False


def write_output_files(protocol_configs: dict[str, list[str]], total_unique: int) -> None:
    """
    Сохраняет результаты по протоколам + общий файл.
    Если за весь прогон не найдено ни одного конфига (полный провал —
    например, все источники недоступны), НИЧЕГО не перезаписывается,
    старые файлы остаются как есть.
    Если конкретный протокол в этом прогоне не дал результатов, но давал
    в прошлый раз — его файл тоже не трогаем (не считаем это провалом).
    """
    if total_unique == 0:
        print(f"{RED}⚠ Не найдено ни одного конфига за весь прогон.{RESET}")
        print(f"{RED}  Файлы НЕ изменены, чтобы не потерять предыдущий результат.{RESET}")
        return

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    all_configs: list[str] = []
    for protocol, configs in protocol_configs.items():
        all_configs.extend(configs)
        if not configs:
            continue  # нет данных по этому протоколу в этом прогоне — не трогаем старый файл
        path = os.path.join(OUTPUT_DIR, f"{protocol}.txt")
        write_atomic(path, "\n".join(configs) + "\n")

    write_atomic(COMBINED_FILE, "\n".join(all_configs) + "\n")


# ─────────────────────────────────────────────
# Главная логика
# ─────────────────────────────────────────────

def main():
    in_ci = is_ci()
    urls = load_urls()

    if not in_ci:
        os.system("cls" if os.name == "nt" else "clear")
        print(f"\n{BOLD}{MAGENTA}{'ТИП':<9} | {'КОЛ-ВО':<8} | ИСТОЧНИК{RESET}")
        print(f"{BLUE}" + "-" * 65 + RESET)
    else:
        print("=== Config Collector starting ===")

    # dict[протокол][ключ_дедупа] = полная_строка  (сохраняет порядок первого появления)
    seen_by_protocol: dict[str, dict[str, str]] = {name: {} for _, name in PROTOCOL_PREFIXES}

    total_raw = 0
    failed_sources = 0

    try:
        for link in urls:
            configs, data_type = parse_configs(link)
            total_raw += len(configs)

            if data_type in ("Net_Err", "Error", "B64_Err", "Empty"):
                failed_sources += 1

            for protocol, line in configs:
                key = dedup_key(line)
                if key not in seen_by_protocol[protocol]:
                    seen_by_protocol[protocol][key] = line

            if not in_ci:
                if data_type in ("Plain", "Base64"):
                    type_col = f"{GREEN}{data_type:<9}{RESET}"
                elif data_type == "Empty":
                    type_col = f"{YELLOW}{data_type:<9}{RESET}"
                else:
                    type_col = f"{RED}{data_type:<9}{RESET}"
                print(f"{type_col} | {len(configs):>4} шт.   | {shorten_url(link)}")
            else:
                print(f"[{data_type}] {len(configs):>4} configs  {shorten_url(link)}")

    except KeyboardInterrupt:
        print(f"\n{YELLOW}⚠  Прервано пользователем (Ctrl+C){RESET}")

    finally:
        protocol_configs = {name: list(d.values()) for name, d in seen_by_protocol.items()}
        total_unique = sum(len(v) for v in protocol_configs.values())

        write_output_files(protocol_configs, total_unique)

        if not in_ci:
            print(f"\n{CYAN}╔{'═' * 58}╗{RESET}")
            print(f"{CYAN}║{RESET}{BOLD}  ВСЕГО НАЙДЕНО:      {total_raw:<36}{RESET}{CYAN}║{RESET}")
            print(f"{CYAN}║{RESET}{BOLD}  УНИКАЛЬНЫХ:         {total_unique:<36}{RESET}{CYAN}║{RESET}")
            print(f"{CYAN}║{RESET}{BOLD}  ИСТОЧНИКОВ С ОШИБК.:{failed_sources:<36}{RESET}{CYAN}║{RESET}")
            print(f"{CYAN}╠{'═' * 58}╣{RESET}")
            for protocol, configs in protocol_configs.items():
                if configs:
                    print(f"{CYAN}║{RESET}    {protocol:<12} : {len(configs):<38}{CYAN}║{RESET}")
            print(f"{CYAN}╚{'═' * 58}╝{RESET}\n")
        else:
            print("\n=== Done ===")
            print(f"Total collected  : {total_raw}")
            print(f"Unique configs   : {total_unique}")
            print(f"Failed sources   : {failed_sources}")
            for protocol, configs in protocol_configs.items():
                if configs:
                    print(f"  {protocol:<10}: {len(configs)}")


if __name__ == "__main__":
    main()
