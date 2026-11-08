#!/usr/bin/env python3
"""
# FL.ru Parser & Analyzer
# Параметры запуска:
#   python3 flparser.py             # один проход: RSS → фильтр → AI → вывод в stdout
#   python3 flparser.py --dry-run   # только показать найденные задачи без AI/записи в БД
#   python3 flparser.py --stats     # показать статистику
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
import html
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.etree import ElementTree

# ─── Базовые пути ────────────────────────────────────────────────────────────
# Директория, где лежит этот скрипт
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "flparser.db"
LOG_DIR = DATA_DIR

# ─── Логирование в файл ─────────────────────────────────────────────────────────
# Логи пишем в data/ — этот каталог смонтирован в docker-compose.yml.
_log_file = LOG_DIR / "flparser.log"
try:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _file_handler = logging.FileHandler(str(_log_file), encoding="utf-8")
except OSError as _e:
    sys.exit(f"[FATAL] Нет доступа к лог-файлу {_log_file}: {_e}")
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_logger = logging.getLogger("flparser")
_logger.addHandler(_file_handler)
_logger.setLevel(logging.DEBUG)

def _log_to_file(msg: str):
    """Записывает сообщение в лог-файл."""
    _logger.debug(msg)

# ─── Загрузка .env ────────────────────────────────────────────────────────────
# Ищем .env в порядке приоритета: рядом со скриптом → /etc/flparser/env
_env_candidates = [
    BASE_DIR / ".env",
    Path("/etc/flparser/env"),
]
for _env_file in _env_candidates:
    if _env_file.exists():
        for _line in _env_file.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ─── Конфигурация через переменные окружения (с дефолтами из кода) ─────────────
# Все скалярные настройки вынесены в .env. Сложные структуры (KEYWORDS, STOP_WORDS,
# AI_PROFILE, AI_PROMPT_TASK_ANALYSIS) остаются в коде.

FL_RU_RSS_URL = os.environ.get("FL_RU_RSS_URL", "https://www.fl.ru/rss/all.xml")

MAX_TASKS_PER_RUN = int(os.environ.get("MAX_TASKS_PER_RUN", "60"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "30"))
AI_TIMEOUT = int(os.environ.get("AI_TIMEOUT", "60"))

URL_RETRY = int(os.environ.get("URL_RETRY", "3"))
URL_DELAY = int(os.environ.get("URL_DELAY", "2"))

PRIORITY_HIGH_THRESHOLD = int(os.environ.get("PRIORITY_HIGH_THRESHOLD", "80"))
PRIORITY_MEDIUM_THRESHOLD = int(os.environ.get("PRIORITY_MEDIUM_THRESHOLD", "60"))

# ─── Telegram ──────────────────────────────────────────────────────────────────
# Для отправки результатов парсинга в чат через Telegram Bot API.

TELEGRAM_BOT_TOKEN = os.environ.get("FL_BOT_TOKEN", "") or os.environ.get("FL_TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("FL_CHAT_ID", "") or os.environ.get("FL_TELEGRAM_CHAT_ID", "")
TG_PROXY = os.environ.get("TG_PROXY", "")

# ─── Прокси ───────────────────────────────────────────────────────────────────
# Отдельные прокси для каждого сервиса. Если не заданы — работаем без прокси.

FL_PROXY = os.environ.get("FL_PROXY", "")
AI_PROXY = os.environ.get("AI_PROXY", "")

# ─── Дебаг ────────────────────────────────────────────────────────────────────
# FLRU_DEBUG2TG=true → в конце stdout добавляется блок "🛠 DEBUG:"
# с расширенной диагностикой + внешний IP через curl ifconfig.co.

DEBUG = os.environ.get("FLRU_DEBUG2TG", "false").lower() in ("1", "true", "yes")

# ─── LLM API ──────────────────────────────────────────────────────────────────
# Универсальный OpenAI-совместимый Chat Completions endpoint.

LLM_API_KEY = os.environ.get(
    "FLRU_LLM_API_KEY",
    "",
) or os.environ.get(
    "OPENAI_API_KEY",
    "",
)
LLM_BASE_URL = os.environ.get(
    "FLRU_LLM_BASE_URL",
    "",
)
LLM_CHAT_URL = f"{LLM_BASE_URL}/chat/completions"

# Модель для анализа задач
AI_MODEL = os.environ.get("FLRU_AI_MODEL", "")

# ─── Проверка конфигурации ─────────────────────────────────────────────────────

LLM_REQUIRED_VARS = {
    "FLRU_LLM_API_KEY": LLM_API_KEY,
    "FLRU_LLM_BASE_URL": LLM_BASE_URL,
    "FLRU_AI_MODEL": AI_MODEL,
}

LLM_CONFIGURED = all(LLM_REQUIRED_VARS.values())

# ─── RSS ──────────────────────────────────────────────────────────────────────

# ─── Ключевые слова для фильтрации ────────────────────────────────────────────

KEYWORDS = [
    "devops", "ci/cd", "kubernetes", "docker", "ansible", "terraform",
    "linux", "облако", "автоматизация", "мониторинг", "vpn", "миграция",
    "yandex cloud", "postgresql", "redis", "nginx", "gitlab", "jenkins",
    "python", "bash", "системное администрирование", "сервер",
    "caddy", "traefik", "helm", "k8s", "docker compose", "dockerfile",
    "iac", "infrastructure", "prometheus", "grafana",
    "loki", "zabbix", "elk", "elasticsearch", "logstash", "kibana",
    "wireguard", "openvpn", "xray", "vless", "selectel",
    "debian", "ubuntu", "centos", "alpine", "rocky",
    "mysql", "mongodb", "mariadb",
    "reverse proxy", "ssl", "tls", "lets encrypt", "certbot",
    "self-hosted", "selfhosted", "деплой", "deploy", "deployment",
    "скрипт", "парсинг", "api", "rest", "webhook",
    "backup", "резервное копирование",
    "настройка", "установка", "конфигурация",
    "администрирование", "maintenance", "обновление",
    "перенос", "migration", "migrate",
    "vagrant", "packer",
    "flask", "django", "fastapi",
    "1с", "1с-битрикс", "bitrix",
    "git", "github", "branching", "code review",
    "alertmanager", "slack", "telegram bot",
    "cron", "scripting", "automation", "orchestration",
    "load balancer", "haproxy", "envoy", "vpn", "ojs",
    "ии-агент", "ai-agent", "ai-агент", "swarm"
]

# ─── Стоп-слова ───────────────────────────────────────────────────────────────

STOP_WORDS = [
    "дизайн", "копирайтинг", "маркетинг", "seo", "контент",
    "тексты", "статьи", "реклама", "smm", "продвижение",
    "перевод", "иллюстрация", "логотип", "бренд",
    "видео", "анимация", "моушн",
    "фото", "обработка фото",
    "vr", "ar", "3d",
    "мобильное приложение", "ios", "android", "flutter", "react native",
    "e-commerce", "магазин", "товары", "каталог",
    "crm", "erp", "hr",
    "seo-оптимизация", "smm-продвижение", "таргет",
    "фейсбук", "facebook", "instagram", "тикток", "tiktok",
    "sound design", "аудио", "подкаст",
    "копирайтер", "редактор", "журналист",
    "аналитик данных", "bi аналитик",
    "учебный", "диплом", "курсовая", "реферат",
    # Рекламные/маркетинговые задачи и вёрстка — не профиль парсера
    "директ", "яндекс директ", "директолог", "google ads", "аутрич",
    "лидогенерация", "таргетолог", "смм",
    "лендинг", "верстка", "tilda", "wordpress", "word press",
]

# ─── Нормализация текста и морфология для фильтра ─────────────────────────────
# Раньше ключевые слова искались простым `keyword in text`, из-за чего:
#   * короткие латинские слова срабатывали внутри других ("api" в произвольном тексте);
#   * общие слова ("настройка", "сервер", "мониторинг", "перенос") пропускали мусор;
#   * стоп-слова не ловили словоформы ("реклама" ≠ "рекламы").
# Теперь поиск идёт по границам слов с простейшей морфологией: у русских слов
# отбрасывается окончание и после основы допускается до 4 букв.

# Сильные ключевые слова — профильные технологии и задачи. Задача проходит фильтр,
# только если найдено хотя бы одно сильное совпадение. Остальные слова из KEYWORDS
# (настройка, сервер, api, мониторинг, автоматизация, парсинг и т.п.) — общие:
# сами по себе они задачу не пропускают, но попадают в keywords_matched.
STRONG_KEYWORDS = {
    # DevOps / IaC / CI-CD
    "devops", "ci/cd", "kubernetes", "k8s", "docker", "docker compose", "dockerfile",
    "ansible", "terraform", "helm", "iac", "infrastructure", "gitlab", "jenkins",
    "vagrant", "packer", "load balancer", "reverse proxy", "orchestration", "swarm",
    # ОС и скриптинг
    "linux", "debian", "ubuntu", "centos", "alpine", "rocky", "bash", "python",
    "flask", "django", "fastapi",
    # Сети, прокси, веб-серверы
    "nginx", "caddy", "traefik", "haproxy", "envoy",
    "vpn", "wireguard", "openvpn", "xray", "vless", "ssl", "tls", "certbot", "lets encrypt",
    # Мониторинг и логирование (конкретные инструменты)
    "prometheus", "grafana", "loki", "zabbix", "elk", "elasticsearch",
    "logstash", "kibana", "alertmanager",
    # Базы данных
    "postgresql", "mysql", "mongodb", "mariadb", "redis",
    # Облака
    "yandex cloud", "selectel",
    # Эксплуатация и миграции
    "backup", "резервное копирование", "миграция", "migration", "migrate",
    "деплой", "deploy", "deployment", "self-hosted", "selfhosted",
    # Прикладные направления из профиля
    "1с", "1с-битрикс", "bitrix", "ии-агент", "ai-agent", "ai-агент",
}

_RU_ENDINGS = tuple(sorted([
    "иями", "ями", "ами", "ыми", "ими", "ого", "его", "ому", "ему",
    "ах", "ях", "ам", "ям", "ом", "ем", "ой", "ей", "ую", "юю", "ых", "их",
    "ов", "ев", "ий", "ый", "ая", "ое", "ые", "ия", "ие", "ии",
    "а", "я", "ы", "и", "е", "у", "ю", "о", "ь",
], key=len, reverse=True))

_CYR_RE = re.compile(r"[а-я]")
_NOT_WORD_RE = re.compile(r"[^0-9a-zа-я+#./_-]+")


def _normalize_text(text: str) -> str:
    """Приводит текст к нижнему регистру и убирает пунктуацию (кроме полезной)."""
    text = (text or "").lower().replace("ё", "е").replace("'", "")
    text = _NOT_WORD_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _ru_stem(word: str) -> str:
    """Отбрасывает типичное русское окончание (основа не короче 4 символов)."""
    for suffix in _RU_ENDINGS:
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _word_pattern(word: str) -> str:
    """Регэксп для одного слова: основа + окончание (рус.) или s/es (лат.)."""
    if _CYR_RE.search(word):
        stem = _ru_stem(word)
        if len(stem) < 4:
            return re.escape(word)
        return re.escape(stem) + r"[а-я]{0,4}"
    return re.escape(word) + r"(?:s|es)?"


def _compile_keyword_pattern(keyword: str) -> re.Pattern:
    """Компилирует ключевое слово/фразу в регэксп с границами слов."""
    parts = [p for p in re.split(r"[\s\-]+", _normalize_text(keyword)) if p]
    body = r"[\s\-]+".join(_word_pattern(p) for p in parts)
    if _CYR_RE.search(keyword):
        return re.compile(r"(?<![а-яa-z0-9])" + body + r"(?![а-яa-z0-9])")
    return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")


def _compile_keywords(keywords) -> list[tuple[str, re.Pattern]]:
    """Готовит список (слово, регэксп), убирая пустые и повторяющиеся слова."""
    compiled = []
    seen = set()
    for keyword in keywords:
        key = _normalize_text(keyword)
        if not key or key in seen:
            continue
        seen.add(key)
        compiled.append((keyword, _compile_keyword_pattern(keyword)))
    return compiled


_KEYWORD_PATTERNS = _compile_keywords(KEYWORDS)
_STOP_PATTERNS = _compile_keywords(STOP_WORDS)


# ─── Профиль DevOps-инженера (system prompt для AI) ─────────────────────────────

AI_PROFILE = """Ты — DevOps-инженер с 25-летним опытом. Твоя специализация:

DevOps и автоматизация:
- CI/CD: GitLab CI (self-hosted), Gitea Actions, Forgejo Actions, Jenkins
- GitOps: Flux CD (основной инструмент), Argo CD
- Deployment стратегии: Blue-green, Canary, Rolling updates

Контейнеризация и оркестрация:
- Docker: экспертный уровень (Dockerfile с нуля, multi-stage builds, оптимизация)
- Docker Compose: основной инструмент для деплоя
- Kubernetes: продвинутый уровень (Managed K8s в Yandex Cloud, Selectel, k3s, k0s)
- Helm: уверенное владение, создание charts

Infrastructure as Code:
- Terraform: уверенное владение, Hybrid Cloud (Yandex + Selectel)
- Ansible: экспертный уровень, Ansible Vault обязательно

Облачные платформы:
- Yandex Cloud: основная специализация, все сервисы
- Selectel: регулярное использование, выгодные цены
- Миграция из западных облаков в российские: ключевая услуга

Мониторинг и логирование:
- Prometheus + Grafana: экспертный уровень
- Loki + Promtail: основной стек логирования в K8s
- Zabbix: официальные курсы, любая настройка

Системное администрирование:
- Linux: экспертный уровень (Ubuntu, CentOS, RHEL, Debian, Alpine, Rocky Linux)
- Windows Server: продвинутый уровень, специализация на 1С и RDS

Базы данных:
- PostgreSQL, MySQL, MariaDB: продвинутый уровень
- MongoDB, Redis: средний уровень

Сети и безопасность:
- VPN: OpenVPN, WireGuard, vless xray
- SSL/TLS: Let's Encrypt, certbot
- Веб-серверы: Caddy, Nginx, Apache, HAProxy, Traefik

Скриптинг и программирование:
- Bash: экспертный уровень
- Python: автоматизация DevOps задач
"""

# ─── AI промпты ──────────────────────────────────────────────────────────────

AI_PROMPT_TASK_ANALYSIS = """Проанализируй задачу как DevOps-инженер:

Заголовок: {title}
Описание: {description}{budget_line}{category_line}
Найденные ключевые слова: {keywords}

Верни ТОЛЬКО JSON без дополнительного текста:
{{"can-do": true/false, "match_score": 0-100, "confidence": 0.0-1.0, "complexity": "simple/medium/complex/unknown", "estimated_hours": число, "recommended_price": число_в_рублях, "relevant_skills": ["навык1", "навык2"], "reasoning": "краткое_объяснение_1-2_предложения"}}"""

# ─── Настройка HTTP-прокси для urllib.request (FL_PROXY) ───────────────────────
# urllib.request не поддерживает прокси напрямую, используем ProxyHandler.
# Применяется ко всем запросам через urllib (RSS, страницы задач).

# ─── База данных (замена Redis) ──────────────────────────────────────────────

def init_db():
    """Инициализация SQLite базы данных."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS processed_ids (
            guid TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guid TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            link TEXT,
            budget INTEGER DEFAULT 0,
            pub_date TEXT,
            category TEXT,
            keywords_matched TEXT,
            ai_can_do INTEGER,
            ai_match_score INTEGER DEFAULT 0,
            ai_confidence REAL DEFAULT 0,
            ai_complexity TEXT,
            ai_estimated_hours REAL DEFAULT 0,
            ai_recommended_price INTEGER DEFAULT 0,
            ai_relevant_skills TEXT,
            ai_reasoning TEXT,
            priority TEXT DEFAULT 'LOW',
            ai_raw_response TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_guid ON tasks(guid);
        CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
        CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
        CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_ids(processed_at);
        """)
        conn.commit()
    except (OSError, sqlite3.Error) as e:
        sys.exit(f"[FATAL] Нет доступа к БД {DB_PATH}: {e}")
    return conn


def is_processed(conn, guid: str) -> bool:
    """Проверяет, была ли задача уже обработана (dedup)."""
    cur = conn.execute("SELECT 1 FROM processed_ids WHERE guid = ?", (guid,))
    return cur.fetchone() is not None


def mark_processed(conn, guid: str):
    """Помечает задачу как обработанную."""
    now = datetime.now(timezone(timedelta(hours=5))).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO processed_ids (guid, processed_at) VALUES (?, ?)",
        (guid, now)
    )


def save_task(conn, task: dict):
    """Сохраняет задачу с AI-анализом в БД."""
    now = datetime.now(timezone(timedelta(hours=5))).isoformat()
    conn.execute("""
        INSERT INTO tasks (
            guid, title, description, link, budget, pub_date, category,
            keywords_matched, ai_can_do, ai_match_score, ai_confidence,
            ai_complexity, ai_estimated_hours, ai_recommended_price,
            ai_relevant_skills, ai_reasoning, priority, ai_raw_response, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        task["guid"],
        task["title"],
        task.get("description", ""),
        task.get("link", ""),
        task.get("budget", 0),
        task.get("pubDate", ""),
        task.get("category", ""),
        json.dumps(task.get("keywords_matched", []), ensure_ascii=False),
        1 if task.get("can_do") else 0,
        task.get("match_score", 0),
        task.get("confidence", 0),
        task.get("complexity", "unknown"),
        task.get("estimated_hours", 0),
        task.get("recommended_price", 0),
        json.dumps(task.get("relevant_skills", []), ensure_ascii=False),
        task.get("reasoning", ""),
        task.get("priority", "LOW"),
        task.get("ai_raw", ""),
        now,
    ))


def cleanup_old_records(conn, days: int = 10):
    """Удаляет записи старше N дней."""
    cutoff = (
        datetime.now(timezone(timedelta(hours=5))) - timedelta(days=days)
    ).isoformat()
    conn.execute("DELETE FROM processed_ids WHERE processed_at < ?", (cutoff,))
    conn.execute("DELETE FROM tasks WHERE created_at < ?", (cutoff,))


# ─── HTTP с retry ─────────────────────────────────────────────────────────────

def _debug_http_request(service_name: str, proxy_url: str):
    """Логирует DEBUG-сообщение о HTTP-запросе с указанием прокси."""
    if not DEBUG:
        return
    if proxy_url:
        print(f"[DEBUG] запрос к {service_name} через прокси")
    else:
        print(f"[DEBUG] запрос к {service_name} напрямую")


def _http_request_with_retry(url, data=None, headers=None, timeout=None, method=None, proxy_url=""):
    """Универсальный HTTP-запрос с retry (URL_RETRY попыток, URL_DELAY сек между ними)."""
    import urllib.request
    import urllib.error
    import time

    timeout = timeout or REQUEST_TIMEOUT
    last_exc = None
    for attempt in range(URL_RETRY):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
            if proxy_url:
                proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                opener = urllib.request.build_opener(proxy_handler)
                with opener.open(req, timeout=timeout) as resp:
                    return resp.read()
            else:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read()
        except Exception as e:
            last_exc = e
            if DEBUG:
                print(f"    [RETRY {attempt+1}/{URL_RETRY}] {type(e).__name__}: {e}", file=sys.stderr)
            if attempt < URL_RETRY - 1:
                time.sleep(URL_DELAY)
    raise last_exc


# ─── Загрузка полного описания задачи ──────────────────────────────────────────

def fetch_task_details(task: dict) -> dict:
    """Загружает полное описание задачи со страницы FL.ru (JSON-LD)."""
    link = task.get("link", "")
    if not link:
        return task

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        _fl_proxy = os.environ.get("FL_PROXY", "") or FL_PROXY
        _debug_http_request("fl.ru", _fl_proxy)
        html_content = _http_request_with_retry(link, headers=headers, timeout=REQUEST_TIMEOUT, proxy_url=_fl_proxy).decode("utf-8", errors="replace")

        # Парсим JSON-LD (application/ld+json)
        json_match = re.search(r'<script\s+type="application/ld\+json">\s*(\{.*?\})\s*</script>', html_content, re.DOTALL)
        if json_match:
            ld_data = json.loads(json_match.group(1))
            full_desc = ld_data.get("description", "")
            if full_desc and len(full_desc) > len(task.get("description", "")):
                task["description"] = full_desc
                task["_full_desc_fetched"] = True
            full_title = ld_data.get("name", "")
            if full_title:
                task["title"] = full_title
    except Exception as e:
        print(f"    [WARN] Не удалось загрузить детали задачи: {e}", file=sys.stderr)

    return task


# ─── RSS Парсер ─────────────────────────────────────────────────────────────────

def fetch_rss(url: str) -> list[dict]:
    """Получает и парсит RSS-ленту fl.ru."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        _fl_proxy = os.environ.get("FL_PROXY", "") or FL_PROXY
        _debug_http_request("fl.ru", _fl_proxy)
        data = _http_request_with_retry(url, headers=headers, timeout=REQUEST_TIMEOUT, proxy_url=_fl_proxy)
        root = ElementTree.fromstring(data)

        items = []
        for item in root.iter("item"):
            def get(tag):
                el = item.find(tag)
                return el.text.strip() if el is not None and el.text else ""

            title_raw = get("title")
            title_decoded = html.unescape(title_raw)
            link = get("link") or get("guid")
            description = html.unescape(get("description"))
            pub_date = get("pubDate")

            categories = []
            for cat in item.findall("category"):
                if cat.text:
                    categories.append(cat.text.strip())

            guid = get("guid") or link

            items.append({
                "guid": guid,
                "title_raw": title_raw,
                "title": title_decoded,
                "description": description,
                "link": link,
                "pubDate": pub_date,
                "categories": categories,
            })

        return items
    except (urllib.error.URLError, ElementTree.ParseError) as e:
        print(f"[ERROR] ⚠  Ошибка при получении RSS!", file=sys.stderr)
        return []


# ─── Фильтрация по ключевым словам ─────────────────────────────────────────────

def extract_budget(title: str) -> tuple[int, str]:
    """Извлекает бюджет из заголовка. FL.ru формат: (Бюджет: 5000 ₽)"""
    match = re.search(r'\(Бюджет:\s*([\d\s]+)\s*[₽$€£]\)', title, re.IGNORECASE)
    if match:
        budget = int(match.group(1).replace(" ", ""))
        clean = title[:match.start()].strip()
        return budget, clean

    match = re.search(r'(\d[\d\s]*)\s*руб', title, re.IGNORECASE)
    if match:
        budget = int(match.group(1).replace(" ", ""))
        clean = title[:match.start()].strip()
        return budget, clean

    return 0, title


def filter_tasks(items: list[dict]) -> list[dict]:
    """Фильтрует задачи по ключевым словам и стоп-словам.

    Стоп-слова ищутся с учётом словоформ; задача проходит, только если найдено
    хотя бы одно сильное ключевое слово (см. STRONG_KEYWORDS).
    """
    filtered = []

    for item in items:
        budget, clean_title = extract_budget(item["title"])
        text = _normalize_text(clean_title + " " + (item.get("description") or ""))

        # Проверяем стоп-слова
        if any(pattern.search(text) for _, pattern in _STOP_PATTERNS):
            continue

        # Ищем совпадения с ключевыми словами
        matched = [kw for kw, pattern in _KEYWORD_PATTERNS if pattern.search(text)]
        if not any(kw in STRONG_KEYWORDS for kw in matched):
            continue

        category = item["categories"][0] if item.get("categories") else "Не указана"

        filtered.append({
            "guid": item["guid"],
            "title": clean_title,
            "description": item.get("description", ""),
            "link": item.get("link", ""),
            "budget": budget,
            "pubDate": item.get("pubDate", ""),
            "category": category,
            "keywords_matched": matched,
        })

    return filtered[:MAX_TASKS_PER_RUN]


# ─── AI Анализ (прямой HTTP к LLM Chat Completions API) ───────────────────────

def _call_openai_api(system_prompt: str, user_message: str, temperature: float = 0.3) -> str:
    """Вызывает LLM Chat Completions API с retry."""
    url = LLM_CHAT_URL
    api_key = LLM_API_KEY
    model = AI_MODEL

    if not api_key or api_key == "***":
        raise RuntimeError("LLM_API_KEY не задан")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
    }

    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": os.environ.get("HERMES_OPENROUTER_APP_REFERER", "https://github.com/ru-admin/flparser"),
        "X-Title": os.environ.get("HERMES_OPENROUTER_APP_TITLE", "FL.ru Parser"),
    }

    _ai_proxy = os.environ.get("AI_PROXY", "") or AI_PROXY
    _debug_http_request("ai", _ai_proxy)

    last_exc = None
    for attempt in range(URL_RETRY):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            if _ai_proxy:
                proxy_handler = urllib.request.ProxyHandler({"http": _ai_proxy, "https": _ai_proxy})
                opener = urllib.request.build_opener(proxy_handler)
                with opener.open(req, timeout=AI_TIMEOUT) as resp:
                    result = json.loads(resp.read())
            else:
                with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as resp:
                    result = json.loads(resp.read())
            return result["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code == 429 and attempt < URL_RETRY - 1:
                wait = (attempt + 1) * URL_DELAY
                print(f"    [RATE LIMIT] 429, retry через {wait}с...", file=sys.stderr)
                time.sleep(wait)
                continue
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM API HTTP {e.code}: {body[:500]}")
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected API response format: {e}")
        except Exception as e:
            last_exc = e
            if DEBUG:
                print(f"    [RETRY {attempt+1}/{URL_RETRY}] {type(e).__name__}: {e}", file=sys.stderr)
            if attempt < URL_RETRY - 1:
                time.sleep(URL_DELAY)
                continue
            raise
    raise last_exc


def analyze_task_with_ai(task: dict) -> dict:
    """Анализирует задачу через AI. При неудаче выбрасывает исключение."""
    budget_line = f"\nБюджет: {task['budget']} руб" if task.get("budget", 0) > 0 else ""
    category_line = f"\nКатегория: {task.get('category', '')}" if task.get("category") else ""

    user_message = AI_PROMPT_TASK_ANALYSIS.format(
        title=task["title"],
        description=task["description"][:2000],
        budget_line=budget_line,
        category_line=category_line,
        keywords=", ".join(task.get("keywords_matched", [])),
    )

    raw = _call_openai_api(AI_PROFILE, user_message, temperature=0.3)

    if not raw:
        raise RuntimeError("Пустой ответ от AI")

    # Извлекаем JSON из ответа
    json_match = re.search(r'\{[\s\S]*\}', raw)
    if not json_match:
        raise RuntimeError("JSON не найден в ответе AI")

    analysis = json.loads(json_match[0])
    analysis["ai_raw"] = raw[:1000]
    return analysis


def set_priority(analysis: dict) -> str:
    """Определяет приоритет на основе match_score."""
    score = analysis.get("match_score", 0)
    if score >= PRIORITY_HIGH_THRESHOLD:
        return "HIGH"
    elif score >= PRIORITY_MEDIUM_THRESHOLD:
        return "MEDIUM"
    return "LOW"


# ─── Статистика ─────────────────────────────────────────────────────────────────

def get_stats(conn, today_only: bool = False) -> dict:
    """Возвращает статистику по задачам (за всё время или только за сегодня).

    Строки «Всего задач» и «За сегодня» всегда считаются по всей БД и не зависят от
    режима — от режима зависят только разбивка по приоритетам и топ задач.
    """
    today = datetime.now(timezone(timedelta(hours=5))).strftime("%Y-%m-%d")
    where = "WHERE date(created_at) = ?" if today_only else ""
    params: tuple = (today,) if today_only else ()

    total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    high = conn.execute(
        f"SELECT COUNT(*) FROM tasks {where}{' AND' if where else ' WHERE'} priority = 'HIGH'",
        params,
    ).fetchone()[0]
    medium = conn.execute(
        f"SELECT COUNT(*) FROM tasks {where}{' AND' if where else ' WHERE'} priority = 'MEDIUM'",
        params,
    ).fetchone()[0]
    low = conn.execute(
        f"SELECT COUNT(*) FROM tasks {where}{' AND' if where else ' WHERE'} priority = 'LOW'",
        params,
    ).fetchone()[0]

    today_count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE date(created_at) = ?", (today,)
    ).fetchone()[0]

    top_tasks = conn.execute(f"""
        SELECT title, guid, budget, ai_match_score, ai_estimated_hours
        FROM tasks {where}{' AND' if where else ' WHERE'} priority = 'HIGH'
        ORDER BY ai_match_score DESC LIMIT 5
    """, params).fetchall()

    return {
        "total": total,
        "high": high,
        "medium": medium,
        "low": low,
        "today": today_count,
        "top_tasks": top_tasks,
    }


def format_stats(stats: dict, today_only: bool = False) -> str:
    """Форматирует статистику для вывода."""
    priority_map = {
        "HIGH": ("🔥", "Высокий приоритет"),
        "MEDIUM": ("⚡", "Средний приоритет"),
        "LOW": ("📌", "Низкий приоритет"),
    }

    msg = "📊 Статистика FL.ru Parser за сегодня:\n\n" if today_only else "📊 Статистика FL.ru Parser:\n\n"
    msg += f"Всего задач: {stats['total']}\n"
    msg += f"🔥 Высокий приоритет: {stats['high']}\n"
    msg += f"⚡ Средний приоритет: {stats['medium']}\n"
    msg += f"📌 Низкий приоритет: {stats['low']}\n"
    msg += f"📅 За сегодня: {stats['today']}\n"

    if stats["top_tasks"]:
        if today_only:
            msg += "\n🏆 Топ задачи за сегодня с высоким приоритетом:\n"
        else:
            msg += "\n🏆 Топ задачи с высоким приоритетом:\n"
        for i, (title, guid, budget, score, hours) in enumerate(stats["top_tasks"], 1):
            budget_str = f"💰 {budget} руб | " if budget > 0 else ""
            short_title = title[:60].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            msg += f'{i}. <a href="{guid}">{short_title}</a> ({score}%)\n'
            msg += f"   {budget_str}⏱️ {hours} ч\n\n"

    return msg


def format_stats_telegram(stats: dict, today_only: bool = False) -> str:
    """Форматирует статистику для отправки в Telegram."""
    msg = "📊 Статистика FL.ru Parser за сегодня:\n\n" if today_only else "📊 Статистика FL.ru Parser:\n\n"
    msg += f"Всего задач: {stats['total']}\n"
    msg += f"🔥 Высокий приоритет: {stats['high']}\n"
    msg += f"⚡ Средний приоритет: {stats['medium']}\n"
    msg += f"📌 Низкий приоритет: {stats['low']}\n"
    msg += f"📅 За сегодня: {stats['today']}\n"

    if stats["top_tasks"]:
        if today_only:
            msg += "\n🏆 Топ задачи за сегодня с высоким приоритетом:\n"
        else:
            msg += "\n🏆 Топ задачи с высоким приоритетом:\n"
        for i, (title, guid, budget, score, hours) in enumerate(stats["top_tasks"], 1):
            short_title = title[:60].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            msg += f'{i}. <a href="{guid}">{short_title}</a> ({score}%)\n'

    return msg[:1500]


# ─── Отправка в Telegram ────────────────────────────────────────────────────────

def _tg_escape(text: str) -> str:
    """Экранирует спецсимволы Telegram HTML."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_to_telegram(message: str):
    """Отправляет сообщение в Telegram через Bot API (без python-telegram-bot)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    _tg_proxy = os.environ.get("TG_PROXY", "") or TG_PROXY
    _debug_http_request("telegram", _tg_proxy)

    text_encoded = urllib.parse.quote(message[:4000])

    url = (
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        f"?chat_id={TELEGRAM_CHAT_ID}"
        f"&text={text_encoded}"
        f"&parse_mode=HTML"
        f"&disable_web_page_preview=true"
    )
    try:
        _http_request_with_retry(url, headers={"User-Agent": "FL.ru Parser/1.0"}, timeout=15, proxy_url=_tg_proxy)
    except Exception as e:
        print(f"[ERROR] ⚠  Не удалось отправить в Telegram!", file=sys.stderr)


def format_task_message(task: dict) -> str:
    """Форматирует сообщение о задаче для Telegram."""
    priority = task.get("priority", "LOW")
    score = task.get("match_score", 0)
    title = task.get("title", "Без названия")
    guid = task.get("guid", "")
    link = task.get("link", "") or guid
    budget = task.get("budget", 0)
    hours = task.get("estimated_hours", 0)
    complexity = task.get("complexity", "unknown")
    skills = task.get("keywords_matched", [])
    description = task.get("description", "")

    priority_map = {
        "HIGH": ("🔥", "Высокий приоритет"),
        "MEDIUM": ("⚡", "Средний приоритет"),
        "LOW": ("📌", "Низкий приоритет"),
    }
    priority_emoji, priority_label = priority_map.get(priority, ("📌", "Низкий приоритет"))

    skills_str = ", ".join(skills) if isinstance(skills, list) else str(skills)

    # Заголовок
    msg = f"{priority_emoji} {priority_label}: {score}%\n"
    msg += f'<a href="{_tg_escape(link)}">{_tg_escape(title)}</a>\n'

    if budget > 0:
        msg += f"💰 Бюджет: {budget:,} руб\n".replace(",", " ")
    msg += f"⏱️ Оценка: {hours} ч\n"
    msg += f"🎯 Сложность: {_tg_escape(complexity)}\n"
    if skills_str:
        msg += f"🔧 Навыки: {_tg_escape(skills_str)}\n"

    # Текст сообщения (после заголовка, до 1500 знаков итого)
    if description:
        msg += f"\n{_tg_escape(description)}"

    if len(msg) > 1500:
        msg = msg[:1500] + "..."

    return msg


# ─── Основной цикл ──────────────────────────────────────────────────────────────

def run_parser(dry_run: bool = False):
    """Основной цикл парсера."""
    start_time = datetime.now()
    mode = " (dry-run)" if dry_run else ""
    print(f"[{start_time.strftime('%Y-%m-%d %H:%M:%S')}] FL.ru Parser запущен{mode}")
    _log_to_file(f"=== Запуск{mode} ===")

    try:
        _run_parser_impl(dry_run)
    finally:
        end_time = datetime.now()
        elapsed = (end_time - start_time).total_seconds()
        print(f"[{end_time.strftime('%Y-%m-%d %H:%M:%S')}] Завершено ({elapsed:.1f}с)")
        _log_to_file(f"=== Завершено за {elapsed:.1f}с ===")


def _run_parser_impl(dry_run: bool):
    """Реализация основного цикла парсера."""
    # Проверка конфигурации LLM
    if not LLM_CONFIGURED:
        missing = [k for k, v in LLM_REQUIRED_VARS.items() if not v]
        error_msg = (
            "❌ Ошибка конфигурации FL.ru Parser!\n"
            f"Не заданы обязательные переменные: {', '.join(missing)}\n"
            "Проверьте env-файл (FLRU_LLM_API_KEY, FLRU_LLM_BASE_URL, FLRU_AI_MODEL).\n"
            "Парсер работает в режиме dry-run (только фильтрация, без записи в БД)."
        )
        print(error_msg)
        if TELEGRAM_BOT_TOKEN:
            send_to_telegram(error_msg)
        dry_run = True

    conn = init_db()
    cleanup_old_records(conn)

    # 1. Получаем RSS
    if DEBUG:
        print("  → Получаем RSS...")
    items = fetch_rss(FL_RU_RSS_URL)
    if DEBUG:
        print(f"  → Получено {len(items)} записей из RSS")

    if not items:
        if DEBUG:
            _debug_block(conn, items, [])
        conn.close()
        return

    # 2. Фильтруем по ключевым словам
    if DEBUG:
        print("  → Фильтруем по ключевым словам...")
    filtered = filter_tasks(items)
    _log_to_file(f"RSS: {len(items)}, после фильтра: {len(filtered)}")
    if DEBUG:
        print(f"  → После фильтрации: {len(filtered)} задач")

    if not filtered:
        if DEBUG:
            _debug_block(conn, items, filtered)
        conn.close()
        return

    # 3. Дедупликация
    new_tasks = [t for t in filtered if not is_processed(conn, t["guid"])]
    _log_to_file(f"Новых задач: {len(new_tasks)}")
    if DEBUG:
        print(f"  → Новых (не обработанных): {len(new_tasks)} задач")

    if not new_tasks:
        if DEBUG:
            _debug_block(conn, items, filtered)
        conn.close()
        return

    # 4. AI анализ — перебор задач
    for i, task in enumerate(new_tasks):
        print(f"  → [{i+1}/{len(new_tasks)}] Анализ: {task['title'][:60]}...")

        if dry_run:
            print(f"    ⏭ dry-run: пропускаем AI-анализ и запись в БД")
            print(f"    ✓ Задача найдена: {task['title'][:80]}")
            continue

        try:
            # Загружаем полное описание задачи со страницы
            task = fetch_task_details(task)
            analysis = analyze_task_with_ai(task)
        except Exception as e:
            print(f"[ERROR] ⚠  AI анализ не удался!", file=sys.stderr)
            print(f"    ⏭ Задача пропущена (не попадёт в БД)")
            continue

        priority = set_priority(analysis)
        task.update(analysis)
        task["priority"] = priority

        mark_processed(conn, task["guid"])
        save_task(conn, task)
        conn.commit()

        # Отправка результата в Telegram
        tg_msg = format_task_message(task)
        send_to_telegram(tg_msg)

        # Детальный вывод в терминал
        hours = analysis.get("estimated_hours", 0)
        complexity = analysis.get("complexity", "unknown")
        reasoning = analysis.get("reasoning", "")
        print(f"    ✓ priority={priority}, score={analysis.get('match_score', 0)}")
        print(f"    ✓ hours={hours}, complexity={complexity}")
        print(f"    💾 id: {task.get('guid', '')}")
        print(f"    🤖 analyze: {reasoning}")

        # Запись в лог-файл
        _log_to_file(f"[{i+1}/{len(new_tasks)}] {task['title']}")
        _log_to_file(f"  priority={priority}, score={analysis.get('match_score', 0)}")
        _log_to_file(f"  hours={hours}, complexity={complexity}")
        _log_to_file(f"  id: {task.get('guid', '')}")
        _log_to_file(f"  analyze: {reasoning}")
        _log_to_file(f"  telegram: sent ({len(tg_msg)} chars)")

    # 5. Опциональный дебаг-блок (FLRU_DEBUG=true)
    if DEBUG:
        _debug_block(conn, items, filtered)

    _log_to_file(f"Готово. Обработано {len(new_tasks)} задач.")

    conn.close()


def _debug_block(conn, items: list[dict], filtered: list[dict]) -> None:
    """Печатает расширенный дебаг-блок. Вызывается только при DEBUG=True."""
    print()
    print("🛠 DEBUG:")

    # 1) Внешние IP через каждый прокси и без прокси (curl ifconfig.co, таймаут 5с)
    proxy_map = {
        "TG_PROXY": TG_PROXY,
        "AI_PROXY": AI_PROXY,
        "FL_PROXY": FL_PROXY,
    }
    for proxy_name, proxy_url in proxy_map.items():
        try:
            req = urllib.request.Request(
                "https://ifconfig.co/",
                headers={"User-Agent": "curl/8.0", "Accept": "*/*"},
            )
            ip = None
            last_err = None
            for attempt in range(URL_RETRY):
                try:
                    if proxy_url:
                        proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                        opener = urllib.request.build_opener(proxy_handler)
                        with opener.open(req, timeout=5) as resp:
                            ip = resp.read().decode("utf-8", errors="replace").strip()
                    else:
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            ip = resp.read().decode("utf-8", errors="replace").strip()
                    break
                except Exception as e:
                    last_err = e
                    if attempt < URL_RETRY - 1:
                        time.sleep(URL_DELAY)
            if ip:
                print(f"  🌐 {proxy_name} IP: {ip}")
            else:
                print(f"  🌐 {proxy_name} IP: ошибка ({type(last_err).__name__}: {last_err})")
        except Exception as e:
            print(f"  🌐 {proxy_name} IP: ошибка ({type(e).__name__}: {e})")

    # 2) RSS / фильтр
    processed_n = conn.execute("SELECT COUNT(*) FROM processed_ids").fetchone()[0]
    print(f"  📥 RSS: {len(items)} записей, после фильтра: {len(filtered)}, "
          f"обработано ранее: {processed_n}")

    # 3) БД: общее/по приоритетам
    today = datetime.now(timezone(timedelta(hours=5))).strftime("%Y-%m-%d")
    row = conn.execute("""
        SELECT
          COUNT(*),
          COALESCE(SUM(CASE WHEN priority='HIGH'   THEN 1 ELSE 0 END), 0),
          COALESCE(SUM(CASE WHEN priority='MEDIUM' THEN 1 ELSE 0 END), 0),
          COALESCE(SUM(CASE WHEN priority='LOW'    THEN 1 ELSE 0 END), 0),
          COALESCE(SUM(CASE WHEN date(created_at)=? THEN 1 ELSE 0 END), 0)
        FROM tasks
    """, (today,)).fetchone()
    total, high, medium, low, today_n = row
    print(f"  💾 БД: всего={total}, HIGH={high}, MEDIUM={medium}, "
          f"LOW={low}, сегодня={today_n}")

    # 4) Последние 5 задач
    last = conn.execute("""
        SELECT title, priority, ai_match_score, ai_estimated_hours, created_at
        FROM tasks ORDER BY created_at DESC LIMIT 5
    """).fetchall()
    if last:
        print("  🕓 Последние 5 задач:")
        for title, prio, score, hours, ts in last:
            short = (title or "")[:50].replace("\n", " ")
            print(f"     • [{prio}] score={score}% hours={hours} | {short}")
    else:
        print("  🕓 БД пуста.")


# ─── Entry Point ────────────────────────────────────────────────────────────────

def _run_stats(today_only: bool = False):
    """Показывает статистику (за всё время или только за сегодня) и шлёт в Telegram."""
    conn = init_db()
    stats = get_stats(conn, today_only=today_only)
    conn.close()
    stats_msg = format_stats(stats, today_only=today_only)
    print(stats_msg)
    _log_to_file("=== --stats-today ===" if today_only else "=== --stats ===")
    _log_to_file(stats_msg)
    send_to_telegram(format_stats_telegram(stats, today_only=today_only))


def main():
    parser = argparse.ArgumentParser(description="FL.ru Parser & Analyzer")
    parser.add_argument("--dry-run", action="store_true",
                        help="Только показать задачи без AI/записи в БД")
    stats_group = parser.add_mutually_exclusive_group()
    stats_group.add_argument("--stats", action="store_true",
                             help="Показать статистику за всё время хранения")
    stats_group.add_argument("--stats-today", action="store_true",
                             help="Показать статистику только за сегодня")
    args = parser.parse_args()

    if args.stats or args.stats_today:
        _run_stats(today_only=args.stats_today)
    else:
        run_parser(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
