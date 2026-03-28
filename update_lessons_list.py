#!/usr/bin/env python3
"""Обновляет lessons_list.txt новыми уроками из кабинета студента LMS."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from time import sleep, time
from typing import Callable

LESSONS_FILE = Path("lessons_list.txt")
RECORDS_URL = "https://lms.itcareerhub.de/local/airtable_schedule/records.php"
BROWSER_STATE_DIR = Path(".browser_profiles")
COOKIE_FILE = BROWSER_STATE_DIR / "lms_cookies.json"


def _report_progress(message: str, progress_cb: Callable[[str], None] | None = None) -> None:
    print(message, flush=True)
    if progress_cb:
        progress_cb(message)


def _load_cookies(driver, cookie_file: Path) -> int:
    if not cookie_file.exists():
        return 0

    try:
        payload = json.loads(cookie_file.read_text(encoding="utf-8"))
    except Exception:
        return 0

    cookies = payload if isinstance(payload, list) else []
    loaded = 0
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        data = dict(cookie)
        data.pop("sameSite", None)
        try:
            driver.add_cookie(data)
            loaded += 1
        except Exception:
            continue
    return loaded


def _save_cookies(driver, cookie_file: Path) -> int:
    try:
        cookies = driver.get_cookies() or []
    except Exception:
        return 0

    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    cookie_file.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(cookies)


def normalize_url(url: str) -> str:
    """Нормализовать Vimeo URL, чтобы стабильно определять дубли."""
    value = (url or "").strip()
    value = re.sub(r"[?#].*$", "", value)
    value = value.rstrip("/")
    return value


def extract_vimeo_id(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""

    patterns = [
        r"player\.vimeo\.com/video/(\d+)",
        r"vimeo\.com/(\d+)",
        r"(?:^|\D)(\d{8,12})(?:\D|$)",
    ]
    for pattern in patterns:
        m = re.search(pattern, value)
        if m:
            return m.group(1)
    return ""


def lesson_dedupe_key(url: str) -> str:
    vid = extract_vimeo_id(url)
    if vid:
        return f"vimeo:{vid}"
    return f"url:{normalize_url(url)}"


def dedupe_lessons(lessons: list[dict[str, str]]) -> list[dict[str, str]]:
    best_by_key: dict[str, dict[str, str]] = {}
    for lesson in lessons:
        key = lesson_dedupe_key(lesson.get("url", ""))
        if key not in best_by_key:
            best_by_key[key] = lesson
            continue

        prev = best_by_key[key]
        if parse_date(lesson.get("date", "")) >= parse_date(prev.get("date", "")):
            best_by_key[key] = lesson
    return list(best_by_key.values())


def parse_date(value: str) -> datetime:
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y")
    except ValueError:
        return datetime.min


def parse_existing_lessons(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []

    lines = path.read_text(encoding="utf-8").splitlines()
    lesson_re = re.compile(r"^\s*\[(?P<date>[^\]]+)\]\s*(?P<title>.+)$")
    subject_re = re.compile(r"^\s{2}(?P<subject>.+?)\s+\(\d+\s+уроков\)\s*$")

    result: list[dict[str, str]] = []
    current_subject = "Unknown"

    i = 0
    while i < len(lines):
        line = lines[i]

        subject_match = subject_re.match(line)
        if subject_match and not line.startswith("  ["):
            current_subject = subject_match.group("subject").strip().title()
            i += 1
            continue

        lesson_match = lesson_re.match(line)
        if lesson_match:
            date = lesson_match.group("date").strip()
            title = lesson_match.group("title").strip()
            teacher = ""
            url = ""

            if i + 1 < len(lines) and "Преподаватель:" in lines[i + 1]:
                teacher = lines[i + 1].split("Преподаватель:", 1)[1].strip()

            if i + 2 < len(lines) and "Видео:" in lines[i + 2]:
                url = lines[i + 2].split("Видео:", 1)[1].strip()

            if url:
                result.append(
                    {
                        "date": date,
                        "title": title,
                        "teacher": teacher,
                        "subject": current_subject,
                        "url": url,
                    }
                )
            i += 3
            continue

        i += 1

    return result


def _count_table_rows_with_vimeo(driver) -> int:
    js = r"""
    const table = document.querySelector('table');
    if (!table) return 0;

    const hasVimeoInText = (value) => {
        const text = String(value || '');
        return /vimeo\.com|player\.vimeo\.com|video\/\d+/i.test(text);
    };

    const rowHasVideo = (row) => {
        if (row.querySelector('a[href*="vimeo.com"],a[href*="player.vimeo.com"]')) return true;
        const dataNodes = Array.from(row.querySelectorAll('[data-whatever]'));
        return dataNodes.some((el) => hasVimeoInText(el.getAttribute('data-whatever')));
    };

    const nodes = Array.from(table.querySelectorAll('tbody tr'));
    return nodes.filter((row) => rowHasVideo(row)).length;
    """
    try:
        return int(driver.execute_script(js) or 0)
    except Exception:
        return 0


def _get_table_state(driver) -> dict[str, int | bool | str]:
    js = r"""
    const table = document.querySelector('table');
    const hasTable = Boolean(table);
    let domRows = 0;
    let dtRows = 0;

    if (table) {
        domRows = table.querySelectorAll('tbody tr').length;
        try {
            if (window.jQuery && jQuery.fn && jQuery.fn.dataTable && jQuery.fn.dataTable.isDataTable(table)) {
                const dt = jQuery(table).DataTable();
                dtRows = dt.rows().count();
            }
        } catch (e) {
            dtRows = 0;
        }
    }

    return {
        has_table: hasTable,
        dom_rows: domRows,
        dt_rows: dtRows,
        href: String(location.href || ''),
        ready_state: String(document.readyState || ''),
    };
    """
    try:
        raw = driver.execute_script(js) or {}
        return {
            "has_table": bool(raw.get("has_table", False)),
            "dom_rows": int(raw.get("dom_rows", 0) or 0),
            "dt_rows": int(raw.get("dt_rows", 0) or 0),
            "href": str(raw.get("href", "") or ""),
            "ready_state": str(raw.get("ready_state", "") or ""),
        }
    except Exception:
        return {"has_table": False, "dom_rows": 0, "dt_rows": 0, "href": "", "ready_state": ""}


def _count_table_rows_total(driver) -> int:
    js = r"""
    const table = document.querySelector('table');
    if (!table) return 0;
    const nodes = Array.from(table.querySelectorAll('tbody tr'));
    return nodes.length;
    """
    try:
        return int(driver.execute_script(js) or 0)
    except Exception:
        return 0


def _looks_like_login_page(driver) -> bool:
    js = r"""
    const user = document.querySelector('input[name="username"], #username, input[type="email"]');
    const pwd = document.querySelector('input[name="password"], #password, input[type="password"]');
    return Boolean(user && pwd);
    """
    try:
        return bool(driver.execute_script(js))
    except Exception:
        return False


def _is_on_records_page(driver, records_url: str) -> bool:
    try:
        current = (driver.current_url or "").split("#", 1)[0].rstrip("/")
    except Exception:
        return False
    target = (records_url or "").split("#", 1)[0].rstrip("/")
    return bool(current and target and current == target)


def scrape_lms_lessons(
    url: str,
    prompt_for_enter: bool = True,
    timeout_sec: int = 900,
    progress_cb: Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    try:
        from selenium import webdriver
    except ImportError as exc:
        raise RuntimeError(
            "selenium не установлен. Запустите: pip install selenium"
        ) from exc

    options = webdriver.EdgeOptions()
    options.add_argument("--disable-blink-features=AutomationControlled")

    driver = webdriver.Edge(options=options)
    driver.set_script_timeout(10)
    try:
        BROWSER_STATE_DIR.mkdir(parents=True, exist_ok=True)
        driver.get("https://lms.itcareerhub.de/")
        loaded_cookies = _load_cookies(driver, COOKIE_FILE)
        if loaded_cookies:
            _report_progress(f"Подгружены сохраненные cookies LMS: {loaded_cookies}.", progress_cb)

        _report_progress("Открываю страницу записей LMS...", progress_cb)
        driver.get(url)

        _report_progress("Страница открыта. Выполните вход в LMS в окне браузера.", progress_cb)

        if prompt_for_enter:
            _report_progress("После входа нажмите Enter в консоли.", progress_cb)
            input("Enter для продолжения: ")
        else:
            _report_progress(f"Жду появления таблицы с видео (до {timeout_sec} сек)...", progress_cb)
            deadline = time() + timeout_sec
            last_report = 0.0
            login_hint_sent = False
            started_wait_at = time()
            last_force_nav_at = 0.0
            while time() < deadline:
                rows_count = _count_table_rows_with_vimeo(driver)
                state = _get_table_state(driver)
                total_rows = max(int(state["dom_rows"]), int(state["dt_rows"]), _count_table_rows_total(driver))
                if rows_count > 0:
                    _report_progress(
                        f"Вход подтвержден. Найдено строк с Vimeo: {rows_count}. Начинаю парсинг таблицы...",
                        progress_cb,
                    )
                    break
                if total_rows > 0:
                    _report_progress(
                        f"Таблица загружена (строк: {total_rows}), начинаю расширенный парсинг видео...",
                        progress_cb,
                    )
                    break
                if state["has_table"] and (time() - started_wait_at) >= 25:
                    _report_progress(
                        "Таблица обнаружена, но строки еще не видны. Пробую парсинг напрямую из DataTables...",
                        progress_cb,
                    )
                    break
                if _looks_like_login_page(driver) and not login_hint_sent:
                    _report_progress(
                        "Обнаружена форма входа LMS: введите логин/пароль в открытом окне браузера.",
                        progress_cb,
                    )
                    login_hint_sent = True

                # LMS может увести на dashboard после логина. Возвращаем пользователя
                # на страницу records автоматически, чтобы парсинг продолжился.
                now = time()
                if (
                    not _looks_like_login_page(driver)
                    and not _is_on_records_page(driver, url)
                    and (now - last_force_nav_at) >= 5
                ):
                    _report_progress("Авторизация обнаружена. Возвращаюсь на страницу записей...", progress_cb)
                    try:
                        driver.get(url)
                    except Exception:
                        pass
                    last_force_nav_at = now

                if now - last_report >= 10:
                    _report_progress(
                        "Ожидаю вход/загрузку таблицы... "
                        f"(state: table={int(state['has_table'])}, dom={state['dom_rows']}, dt={state['dt_rows']})",
                        progress_cb,
                    )
                    last_report = now
                sleep(2)
            else:
                raise TimeoutError(
                    "Не удалось дождаться строк с Vimeo в таблице LMS. "
                    "Проверьте вход в кабинет и доступ к записям."
                )

        saved_cookies = _save_cookies(driver, COOKIE_FILE)
        if saved_cookies:
            _report_progress(f"Сессия сохранена (cookies: {saved_cookies}).", progress_cb)

        js = r"""
        const table = document.querySelector('table');
        if (!table) return [];

        const normalizeVimeoUrl = (raw) => {
            const value = String(raw || '').trim();
            if (!value) return '';

            // Raw LMS data-whatever may appear either as a raw value or inside full HTML:
            //   1177707919?h=f6e33e3541
            //   ... data-whatever="1177707919?h=f6e33e3541" ...
            const idWithHash = value.match(/(\d{8,12})[^"'\s<>]*[?&]h=([a-f0-9]{6,})/i);
            if (idWithHash) return `https://vimeo.com/${idWithHash[1]}/${idWithHash[2]}`;

            if (/^\d{8,12}$/.test(value)) return `https://vimeo.com/${value}`;

            // player.vimeo.com/video/ID?h=HASH  → vimeo.com/ID/HASH
            const playerH = value.match(/player\.vimeo\.com\/video\/(\d+)[^"'\s<>]*[?&]h=([a-f0-9]{6,})/i);
            if (playerH) return `https://vimeo.com/${playerH[1]}/${playerH[2]}`;

            // Full https:// URL — return as-is (preserves /HASH path segment)
            const direct = value.match(/https?:\/\/[^"'\s<>]+/i);
            if (direct) return direct[0];

            // vimeo.com/ID/HASH or vimeo.com/ID (no scheme)
            const idMatch = value.match(/(?:video\/|vimeo\.com\/)(\d{6,})(?:\/([a-f0-9]{6,}))?/i);
            if (idMatch) return idMatch[2] ? `https://vimeo.com/${idMatch[1]}/${idMatch[2]}` : `https://vimeo.com/${idMatch[1]}`;

            const looseId = value.match(/(?:^|\D)(\d{8,12})(?:\D|$)/);
            if (looseId) return `https://vimeo.com/${looseId[1]}`;

            return '';
        };

        const getRowVideoUrl = (row) => {
            // Iframes come first — they contain the full player URL with ?h=HASH
            const iframe = row.querySelector('iframe[src*="vimeo"]');
            if (iframe && iframe.src) {
                const parsed = normalizeVimeoUrl(iframe.src);
                if (parsed) return parsed;
            }

            const link = row.querySelector('a[href*="vimeo.com"],a[href*="player.vimeo.com"]');
            if (link && link.href) {
                const parsed = normalizeVimeoUrl(link.href);
                if (parsed) return parsed;
            }

            const dataNodes = Array.from(row.querySelectorAll('[data-whatever]'));
            for (const node of dataNodes) {
                const parsed = normalizeVimeoUrl(node.getAttribute('data-whatever'));
                if (parsed) return parsed;
            }

            // Fallback: scan all attributes in the row for embedded video ids/urls.
            const allNodes = Array.from(row.querySelectorAll('*'));
            for (const node of allNodes) {
                const attrs = node.getAttributeNames ? node.getAttributeNames() : [];
                for (const attrName of attrs) {
                    const attrVal = node.getAttribute(attrName);
                    const parsed = normalizeVimeoUrl(attrVal);
                    if (parsed) return parsed;
                }
            }

            return '';
        };

        const getVideoFromCellHtml = (value) => {
            const html = String(value || '');
            if (!html) return '';

            const parsedFromRaw = normalizeVimeoUrl(html);
            if (parsedFromRaw) return parsedFromRaw;

            const wrap = document.createElement('div');
            wrap.innerHTML = html;

            const link = wrap.querySelector('a[href]');
            if (link) {
                const parsed = normalizeVimeoUrl(link.getAttribute('href') || link.href || '');
                if (parsed) return parsed;
            }

            const dataNodes = Array.from(wrap.querySelectorAll('[data-whatever]'));
            for (const node of dataNodes) {
                const parsed = normalizeVimeoUrl(node.getAttribute('data-whatever'));
                if (parsed) return parsed;
            }

            const allNodes = Array.from(wrap.querySelectorAll('*'));
            for (const node of allNodes) {
                const attrs = node.getAttributeNames ? node.getAttributeNames() : [];
                for (const attrName of attrs) {
                    const parsed = normalizeVimeoUrl(node.getAttribute(attrName));
                    if (parsed) return parsed;
                }
            }

            return '';
        };

        const textFromCellHtml = (value) => {
            const wrap = document.createElement('div');
            wrap.innerHTML = String(value || '');
            return (wrap.textContent || '').trim();
        };

        const collectFromDataTable = () => {
            try {
                if (!(window.jQuery && jQuery.fn && jQuery.fn.dataTable && jQuery.fn.dataTable.isDataTable(table))) {
                    return [];
                }
                const dt = jQuery(table).DataTable();
                const rawRows = dt.rows().data().toArray();

                return rawRows
                    .map((row) => {
                        const cells = Array.isArray(row) ? row : Object.values(row || {});
                        if (cells.length < 6) return null;

                        let url = '';
                        for (const cell of cells) {
                            url = getVideoFromCellHtml(cell);
                            if (url) break;
                        }
                        if (!url) return null;

                        return {
                            title: textFromCellHtml(cells[1]) || '',
                            subject: textFromCellHtml(cells[2]) || 'Unknown',
                            teacher: textFromCellHtml(cells[3]) || '',
                            date: textFromCellHtml(cells[4]) || '',
                            url: url,
                        };
                    })
                    .filter(Boolean);
            } catch (e) {
                return [];
            }
        };

        const fromDataTable = collectFromDataTable();
        if (fromDataTable.length) return fromDataTable;

        let nodes = [];
        try {
            if (window.jQuery && jQuery.fn && jQuery.fn.dataTable) {
                const dt = jQuery(table).DataTable();
                nodes = dt.rows().nodes().toArray();
            }
        } catch (e) {
            nodes = [];
        }

        if (!nodes.length) {
            nodes = Array.from(table.querySelectorAll('tbody tr'));
        }

        return nodes
            .map((row) => {
                const cells = Array.from(row.querySelectorAll('td')).map((x) => x.innerText.trim());
                if (cells.length < 6) return null;

                const url = getRowVideoUrl(row);
                if (!url) return null;

                return {
                    title: cells[1] || '',
                    subject: cells[2] || 'Unknown',
                    teacher: cells[3] || '',
                    date: cells[4] || '',
                    url: url
                };
            })
            .filter(Boolean);
        """

        rows = driver.execute_script(js) or []
        _report_progress(f"Парсинг таблицы завершен. Получено строк: {len(rows)}.", progress_cb)
    finally:
        driver.quit()

    cleaned: list[dict[str, str]] = []
    seen = set()
    for row in rows:
        item = {
            "date": (row.get("date") or "").strip(),
            "title": (row.get("title") or "").strip(),
            "teacher": (row.get("teacher") or "").strip(),
            "subject": (row.get("subject") or "Unknown").strip().title(),
            "url": (row.get("url") or "").strip(),
        }
        if not item["url"]:
            continue
        key = lesson_dedupe_key(item["url"])
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)

    _report_progress(f"После очистки и дедупликации осталось уроков: {len(cleaned)}.", progress_cb)
    return cleaned


def update_lessons_file(
    output: Path = LESSONS_FILE,
    records_url: str = RECORDS_URL,
    prompt_for_enter: bool = True,
    timeout_sec: int = 900,
    progress_cb: Callable[[str], None] | None = None,
) -> dict[str, int | str]:
    _report_progress("Читаю текущий lessons_list.txt...", progress_cb)
    existing_raw = parse_existing_lessons(output)
    existing = dedupe_lessons(existing_raw)
    removed_duplicates = len(existing_raw) - len(existing)
    _report_progress(
        f"Текущих уроков в файле: {len(existing_raw)} (после дедупликации: {len(existing)}).",
        progress_cb,
    )
    if removed_duplicates > 0:
        _report_progress(f"Удалено дублей из текущего списка: {removed_duplicates}.", progress_cb)
    fresh = scrape_lms_lessons(
        records_url,
        prompt_for_enter=prompt_for_enter,
        timeout_sec=timeout_sec,
        progress_cb=progress_cb,
    )

    existing_by_url = {lesson_dedupe_key(x["url"]): x for x in existing}
    added = 0
    updated = 0
    for lesson in fresh:
        key = lesson_dedupe_key(lesson["url"])
        if key in existing_by_url:
            old = existing_by_url[key]
            # Update URL if the fresh one contains a hash and the stored one doesn't
            old_has_hash = re.search(r'/[a-f0-9]{6,}(?:[?#]|$)', old["url"])
            new_has_hash = re.search(r'/[a-f0-9]{6,}(?:[?#]|$)', lesson["url"])
            if new_has_hash and not old_has_hash:
                old["url"] = lesson["url"]
                updated += 1
            continue
        existing.append(lesson)
        existing_by_url[key] = lesson
        added += 1

    existing = dedupe_lessons(existing)

    _report_progress("Сохраняю обновленный lessons_list.txt...", progress_cb)
    output.write_text(render_lessons(existing), encoding="utf-8")
    _report_progress(
        f"Готово. Найдено на LMS: {len(fresh)}, добавлено новых: {added}, обновлено URL: {updated}, всего в файле: {len(existing)}.",
        progress_cb,
    )
    return {
        "found": len(fresh),
        "added": added,
        "updated": updated,
        "total": len(existing),
        "removed_duplicates": removed_duplicates,
        "output": str(output),
    }


def render_lessons(lessons: list[dict[str, str]]) -> str:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for lesson in lessons:
        grouped[lesson["subject"]].append(lesson)

    subjects = sorted(grouped.keys())
    total = len(lessons)

    out: list[str] = []
    out.append("=" * 80)
    out.append("ЗАПИСИ ЗАНЯТИЙ — itcareerhub.de")
    out.append(f"Всего уроков: {total}")
    out.append("=" * 80)
    out.append("")

    for subject in subjects:
        items = grouped[subject]
        items.sort(key=lambda x: (parse_date(x["date"]), x["title"].lower()), reverse=True)

        out.append("=" * 80)
        out.append(f"  {subject.upper()}  ({len(items)} уроков)")
        out.append("=" * 80)

        for lesson in items:
            out.append(f"  [{lesson['date']}] {lesson['title']}")
            out.append(f"    Преподаватель: {lesson['teacher']}")
            out.append(f"    Видео: {lesson['url']}")
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Обновить lessons_list.txt новыми видео из LMS")
    parser.add_argument("--records-url", default=RECORDS_URL, help="URL страницы с записями")
    parser.add_argument("--output", default=str(LESSONS_FILE), help="Файл списка уроков")
    args = parser.parse_args()

    stats = update_lessons_file(
        output=Path(args.output),
        records_url=args.records_url,
        prompt_for_enter=True,
    )

    print(f"Найдено на LMS: {stats['found']}")
    print(f"Добавлено новых: {stats['added']}")
    print(f"Итого в файле: {stats['total']}")
    print(f"Сохранено: {stats['output']}")


if __name__ == "__main__":
    main()
