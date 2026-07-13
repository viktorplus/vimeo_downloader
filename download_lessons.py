#!/usr/bin/env python3
"""
Скрипт для скачивания видеоуроков с Vimeo.
Использование:
    python download_lessons.py <url>
    python download_lessons.py <url> --quality 720
    python download_lessons.py --file urls.txt
"""

import argparse
import glob
import importlib.util
import os
import re
import shutil
import sys
from pathlib import Path

try:
    import yt_dlp
    from yt_dlp.networking.impersonate import ImpersonateTarget
except ImportError:
    print("Установите yt-dlp: pip install yt-dlp")
    sys.exit(1)


def _find_ffmpeg() -> str | None:
    """Найти ffmpeg: сначала в PATH, затем в стандартных winget/scoop-директориях."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    # Winget устанавливает в AppData пользователя — ищем там
    winget_base = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
    )
    pattern = os.path.join(winget_base, "**", "bin", "ffmpeg.exe")
    matches = glob.glob(pattern, recursive=True)
    if matches:
        return matches[0]
    return None


def list_formats(url: str):
    """Показать доступные форматы для URL."""
    ydl_opts = {"listformats": True, "quiet": False}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.extract_info(url, download=False)


ARCHIVE_FILE = Path(".downloaded")


def _has_module(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _vimeo_id_from_url(url: str) -> str | None:
    """Extract the numeric Vimeo video ID from a URL."""
    value = (url or "").strip()
    if not value:
        return None

    patterns = [
        r"player\.vimeo\.com/video/(\d+)",
        r"vimeo\.com/(\d+)",
        r"(?:^|\D)(\d{8,12})(?:\D|$)",
    ]
    for pattern in patterns:
        m = re.search(pattern, value)
        if m:
            return m.group(1)
    return None


def _already_downloaded(url: str) -> bool:
    """Return True if this URL is already in the local archive — no network needed."""
    if not ARCHIVE_FILE.exists():
        return False
    vid_id = _vimeo_id_from_url(url)
    if not vid_id:
        return False
    # yt-dlp archive format: one "extractor id" per line, e.g. "vimeo 1177259390"
    return f"vimeo {vid_id}" in ARCHIVE_FILE.read_text(encoding="utf-8")


_FS_FORBIDDEN_REPLACEMENTS = {
    ":": "：",   # FULLWIDTH COLON
    "*": "＊",
    "?": "？",
    '"': "＂",
    "<": "＜",
    ">": "＞",
    "|": "｜",
    "/": "／",
    "\\": "＼",
}


def _sanitize_filename(name: str) -> str:
    """Подготовить имя для outtmpl yt-dlp с fullwidth заменами.

    Раньше полагались на yt-dlp: он сам заменяет : * ? < > | " на fullwidth.
    Но при использовании в outtmpl yt-dlp читает PATH целиком и встречает ':'
    как drive-separator → заменяет на '#' (видимо, чтобы не сломать Windows
    путь). Поэтому делаем замены сами заранее.

    Дополнительно: '%' защищаем от шаблонов yt-dlp, контрол-символы убираем,
    приводим к Unicode NFC (Windows и Linux хранят имена в этой форме;
    NFD создавал бы дубли с визуально одинаковым именем).
    """
    if not name:
        return "video"
    import unicodedata
    cleaned = unicodedata.normalize("NFC", name)
    cleaned = cleaned.replace("%", "％")
    for src, dst in _FS_FORBIDDEN_REPLACEMENTS.items():
        cleaned = cleaned.replace(src, dst)
    cleaned = re.sub(r"[\x00-\x1f]", "_", cleaned)
    cleaned = cleaned.strip(" .")
    return cleaned[:200] if cleaned else "video"


def _base_ydl_opts(
    quality: int,
    output_dir: Path,
    fast: bool,
    has_ffmpeg: bool,
    ffmpeg_path: str | None,
    filename_stem: str | None = None,
) -> dict:
    if fast:
        format_selector = f"best[height<={quality}]/best"
    elif has_ffmpeg:
        format_selector = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
    else:
        format_selector = f"best[height<={quality}]/best"

    if filename_stem:
        # Force literal stem — yt-dlp templates would re-interpret %()s sequences.
        outtmpl = str(output_dir / filename_stem) + ".%(ext)s"
    else:
        outtmpl = str(output_dir / "%(title)s.%(ext)s")

    opts = {
        "format": format_selector,
        "outtmpl": outtmpl,
        "progress_hooks": [progress_hook],
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://vimeo.com/",
        },
        "download_archive": str(ARCHIVE_FILE),
    }

    if has_ffmpeg and not fast and ffmpeg_path:
        opts["ffmpeg_location"] = str(Path(ffmpeg_path).parent)
        opts["merge_output_format"] = "mp4"

    return opts


def _candidate_download_urls(url: str) -> list[str]:
    """Return list of URLs to try: original first, then standard forms.

    Converts player.vimeo.com/video/ID?h=HASH to canonical vimeo.com/ID/HASH.
    """
    # Extract video ID and optional privacy hash from any Vimeo URL form
    vid = _vimeo_id_from_url(url)

    # Try to find the privacy hash: either as path segment /HASH or query param ?h=HASH
    hash_val: str | None = None
    if vid:
        # Path form: vimeo.com/ID/HASH
        path_hash = re.search(rf'{re.escape(vid)}/([a-f0-9]{{6,}})', url, re.I)
        if path_hash:
            hash_val = path_hash.group(1)
        else:
            # Query param form: ?h=HASH or &h=HASH
            q_hash = re.search(r'[?&]h=([a-f0-9]{6,})', url, re.I)
            if q_hash:
                hash_val = q_hash.group(1)

    # LMS иногда отдаёт фейковый "хэш", равный самому ID (data-whatever="ID?h=ID").
    # Такой хэш не существует на Vimeo → 404. Отбрасываем его и качаем vimeo.com/ID.
    if hash_val and vid and hash_val == vid:
        hash_val = None

    candidates: list[str] = []
    if vid and hash_val:
        # Prefer canonical vimeo.com/ID/HASH form
        canonical = f"https://vimeo.com/{vid}/{hash_val}"
        player_h = f"https://player.vimeo.com/video/{vid}?h={hash_val}"
        for item in (canonical, player_h):
            if item not in candidates:
                candidates.append(item)
        if url not in candidates:
            candidates.append(url)
    else:
        candidates.append(url)
        if vid:
            for item in (
                f"https://player.vimeo.com/video/{vid}",
                f"https://vimeo.com/{vid}",
            ):
                if item not in candidates:
                    candidates.append(item)
    return candidates


def _build_attempts(base_opts: dict) -> list[dict]:
    """Return list of ydl_opts dicts to try, in order of preference.

    1. Plain — works for hash-protected private videos
    2. Impersonation — fallback for TLS-restricted IPs
    """
    attempts: list[dict] = [dict(base_opts)]

    if _has_module("curl_cffi"):
        a = dict(base_opts)
        a["impersonate"] = ImpersonateTarget.from_str("chrome-120")
        attempts.append(a)

    return attempts


def _mirror_to(src: Path, extra_dirs: list[Path]) -> None:
    """Скопировать src в каждую папку из extra_dirs (если там ещё нет).

    Ошибки копирования логируем, но не падаем — основная скачка важнее.
    """
    if not extra_dirs:
        return
    for ed in extra_dirs:
        try:
            ed.mkdir(parents=True, exist_ok=True)
            dst = ed / src.name
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                continue
            if dst.exists():
                # Размер отличается — не перезаписываем, чтобы не сломать существующее
                print(f"  Зеркало   : пропущено (есть файл другого размера) {dst}")
                continue
            shutil.copy2(src, dst)
            print(f"  Зеркало   : скопировано в {ed}")
        except Exception as exc:
            print(f"  Зеркало   : ошибка для {ed}: {exc}")


def _find_downloaded_file(output_dir: Path, filename_stem: str | None) -> Path | None:
    if not filename_stem:
        return None
    # filename_stem уже прошёл _sanitize_filename — fullwidth-замены сделаны,
    # yt-dlp не должен ничего менять при записи на диск.
    for ext in (".mp4", ".mkv", ".webm", ".m4v", ".mov"):
        candidate = output_dir / f"{filename_stem}{ext}"
        if candidate.exists():
            return candidate
    return None


def download(
    url: str,
    quality: int,
    output_dir: Path,
    fast: bool = False,
    lesson_title: str | None = None,
    extra_output_dirs: list[Path] | None = None,
) -> bool:
    """Скачать видео по URL.

    fast=True — скачать единым progressive-потоком (быстро, без склейки).
    fast=False — скачать раздельные видео+аудио и смёрджить через ffmpeg (лучшее качество).
    lesson_title — если задан, используется как имя файла (вместо названия из Vimeo).
        Защищает от коллизий, когда у нескольких видео одинаковый заголовок на Vimeo.
    extra_output_dirs — список папок-зеркал. Файл копируется в каждую сразу после
        успешной скачки. Имя файла внутри папок совпадает с основной output_dir.

    Возвращает True если видео скачано, False если пропущено (уже было скачано ранее).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    filename_stem = _sanitize_filename(lesson_title) if lesson_title else None
    extras: list[Path] = list(extra_output_dirs or [])
    # Не зеркалим в саму output_dir, если она же оказалась в списке
    extras = [p for p in extras if Path(p).resolve() != Path(output_dir).resolve()]

    # Проверяем архив локально — без единого запроса к Vimeo
    if _already_downloaded(url):
        print(f"\n  Пропущено : {url}")
        print("              уже скачан ранее (запись в .downloaded)\n")
        # Даже если уже скачано — досинхронизируем в зеркала, если там нет файла
        existing = _find_downloaded_file(output_dir, filename_stem)
        if existing is not None:
            _mirror_to(existing, extras)
        return False

    ffmpeg_path = _find_ffmpeg()
    has_ffmpeg = ffmpeg_path is not None

    if not has_ffmpeg and not fast:
        print("\n  Внимание  : ffmpeg не найден, используем режим без склейки потоков")
        print("              Для максимального качества установите ffmpeg\n")

    base_opts = _base_ydl_opts(
        quality, output_dir, fast, has_ffmpeg, ffmpeg_path, filename_stem=filename_stem
    )
    attempts = _build_attempts(base_opts)
    candidates = _candidate_download_urls(url)
    referers = [
        "https://lms.itcareerhub.de/local/airtable_schedule/records.php",
        "https://vimeo.com/",
    ]

    last_error: Exception | None = None
    for attempt_opts in attempts:
        for candidate in candidates:
            for referer in referers:
                ydl_opts = dict(attempt_opts)
                ydl_opts["http_headers"] = {
                    **base_opts.get("http_headers", {}),
                    "Referer": referer,
                }
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(candidate, download=False)
                        title = info.get("title", candidate)
                        browser = attempt_opts.get("cookiesfrombrowser", (None,))[0]
                        impersonate = attempt_opts.get("impersonate")
                        mode = ", ".join(filter(None, [
                            f"cookies:{browser}" if browser else None,
                            "impersonate" if impersonate else None,
                        ])) or "plain"
                        print(f"\n  Заголовок : {title}")
                        print(f"  Качество  : до {quality}p")
                        print(f"  Папка     : {output_dir}")
                        if extras:
                            print(f"  Зеркала   : {', '.join(str(p) for p in extras)}")
                        print(f"  Источник  : {candidate}")
                        print(f"  Referer   : {referer}")
                        print(f"  Режим     : {mode}\n")
                        # Ensure quiet=False for the actual download so progress shows
                        ydl.params["quiet"] = False
                        ydl.download([candidate])
                        produced = _find_downloaded_file(output_dir, filename_stem)
                        if produced is not None:
                            _mirror_to(produced, extras)
                        return True
                except Exception as exc:
                    last_error = exc
                    continue

    if last_error:
        raise last_error
    raise RuntimeError("Не удалось скачать видео: неизвестная ошибка yt-dlp")


def progress_hook(d: dict):
    if d["status"] == "finished":
        print(f"\n  Готово: {d['filename']}")


def main():
    parser = argparse.ArgumentParser(
        description="Скачать видеоурок с Vimeo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python download_lessons.py "https://vimeo.com/1177373233/a17469db60?fl=pl&fe=cm"
  python download_lessons.py "https://vimeo.com/..." --quality 720 --output ~/Videos/lessons
  python download_lessons.py --file urls.txt --quality 1080
  python download_lessons.py "https://vimeo.com/..." --list-formats
        """,
    )
    parser.add_argument("url", nargs="?", help="URL видео на Vimeo")
    parser.add_argument(
        "--file", "-f", help="Текстовый файл со списком URL (по одному на строку)"
    )
    parser.add_argument(
        "--quality", "-q", type=int, default=1080,
        choices=[360, 480, 720, 1080, 1440, 2160],
        help="Максимальное качество в px (по умолчанию: 1080)",
    )
    parser.add_argument(
        "--output", "-o", default="./videos",
        help="Папка для сохранения (по умолчанию: ./videos)",
    )
    parser.add_argument(
        "--list-formats", "-l", action="store_true",
        help="Показать доступные форматы без скачивания",
    )

    args = parser.parse_args()

    # Собрать список URL
    urls: list[str] = []
    if args.url:
        urls.append(args.url)
    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"Файл не найден: {args.file}")
            sys.exit(1)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)

    if not urls:
        parser.print_help()
        sys.exit(1)

    output_dir = Path(args.output)

    for i, url in enumerate(urls, 1):
        print(f"\n[{i}/{len(urls)}] {url}")
        if args.list_formats:
            list_formats(url)
        else:
            try:
                download(url, args.quality, output_dir)
            except yt_dlp.utils.DownloadError as e:
                print(f"  Ошибка скачивания: {e}")
            except Exception as e:
                print(f"  Неожиданная ошибка: {e}")


if __name__ == "__main__":
    main()
