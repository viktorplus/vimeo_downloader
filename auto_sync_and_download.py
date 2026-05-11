"""Полностью автоматический сценарий:
1) обновить lessons_list.txt из LMS (автологин по .env);
2) скачать все уроки, которых ещё нет в архиве .downloaded.

Параметры берутся из .env: LMS_LOGIN, LMS_PASSWORD, QUALITY, FAST.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import update_lessons_list as u  # noqa: E402
import lessons_menu as m  # noqa: E402
from config import load_env, get_int, get_bool  # noqa: E402
from download_lessons import download  # noqa: E402

LESSONS_FILE = ROOT / "lessons_list.txt"
VIDEOS_DIR = ROOT / "videos"
ARCHIVE_FILE = ROOT / ".downloaded"


def _archive_ids() -> set[str]:
    if not ARCHIVE_FILE.exists():
        return set()
    ids: set[str] = set()
    for line in ARCHIVE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("vimeo "):
            ids.add(line.split(" ", 1)[1].strip())
    return ids


def main() -> int:
    env = load_env()
    login = env.get("LMS_LOGIN", "")
    password = env.get("LMS_PASSWORD", "")
    quality = get_int(env, "QUALITY", 1080)
    fast = get_bool(env, "FAST", False)

    if not login or not password:
        print("ОШИБКА: в .env должны быть указаны LMS_LOGIN и LMS_PASSWORD")
        print("Скопируйте .env.example в .env и заполните поля.")
        return 1

    print("=" * 70)
    print("ШАГ 1/2: обновление списка уроков из LMS")
    print("=" * 70)
    print(f"Логин   : {login}")
    print(f"Качество: до {quality}p")
    print(f"Режим   : {'быстро (без склейки)' if fast else 'максимум (видео+аудио через ffmpeg)'}")
    print()

    sync_stats = u.update_lessons_file(
        output=LESSONS_FILE,
        prompt_for_enter=False,
        login=login,
        password=password,
    )
    print(
        f"Список обновлён. Всего в файле: {sync_stats['total']}, "
        f"добавлено новых: {sync_stats['added']}."
    )
    print()

    print("=" * 70)
    print("ШАГ 2/2: скачивание новых видео")
    print("=" * 70)

    lessons = u.parse_existing_lessons(LESSONS_FILE)
    already = _archive_ids()
    new_lessons: list[dict[str, str]] = []
    for lesson in lessons:
        vid = u.extract_vimeo_id(lesson.get("url", ""))
        if vid and vid not in already:
            new_lessons.append(lesson)

    if not new_lessons:
        print("Новых уроков нет — всё уже скачано.")
        return 0

    print(f"Уроков к скачиванию: {len(new_lessons)}")
    success = 0
    skipped = 0
    failed: list[tuple[dict[str, str], str]] = []

    for idx, lesson in enumerate(new_lessons, start=1):
        subj = m.sanitize_folder_name(lesson.get("subject", ""))
        out_dir = VIDEOS_DIR / subj
        title = lesson.get("title", "")
        print(f"\n[{idx}/{len(new_lessons)}] [{lesson.get('subject', '')}] {title}")
        try:
            ok = download(
                lesson["url"],
                quality=quality,
                output_dir=out_dir,
                fast=fast,
                lesson_title=title,
            )
            if ok:
                success += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001
            failed.append((lesson, str(exc)))
            print(f"  ОШИБКА: {exc}")

    print()
    print("=" * 70)
    print("ИТОГ")
    print("=" * 70)
    print(f"Список   : всего {sync_stats['total']}, новых добавлено {sync_stats['added']}")
    print(f"Скачано  : {success}")
    print(f"Пропущено: {skipped} (yt-dlp пометил как already-downloaded)")
    print(f"Ошибок   : {len(failed)}")
    if failed:
        for lesson, err in failed:
            print(f"  [{lesson.get('subject', '')}] {lesson.get('title', '')}")
            print(f"    {err}")

    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
