"""Одноразовая миграция: переименовать существующие файлы и докачать недостающие.

Использует те же правила именования что и обновлённый lessons_menu.py:
- имя файла = sanitize_filename(lesson.title)
- при коллизии (несколько уроков с одинаковым названием в одном предмете) —
  добавляется дата урока для уникальности.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VIDEOS = ROOT / "videos"
LESSONS_FILE = ROOT / "lessons_list.txt"
ARCHIVE_FILE = ROOT / ".downloaded"

sys.path.insert(0, str(ROOT))
import update_lessons_list as u
import lessons_menu as m
from download_lessons import _sanitize_filename, download
from yt_dlp.utils import sanitize_filename as _ytdlp_sanitize


def expected_filename_stem(raw_title: str) -> str:
    """Имя файла как сохранил бы yt-dlp с outtmpl=%(title)s."""
    return _ytdlp_sanitize(_sanitize_filename(raw_title), restricted=False)


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".m4v"}


def compute_expected_stems(lessons: list[dict[str, str]]) -> dict[str, str]:
    """Для каждого урока вычислить итоговый stem имени файла.

    Уникален в пределах подпапки subject: при коллизии добавляется дата.
    """
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for lesson in lessons:
        sf = m.sanitize_folder_name(lesson.get("subject", ""))
        nt = m.normalize_media_name(lesson.get("title", ""))
        groups[(sf, nt)].append(lesson)

    stems: dict[str, str] = {}
    for (_sf, _nt), group in groups.items():
        if len(group) == 1:
            stems[group[0]["url"]] = expected_filename_stem(group[0].get("title", ""))
        else:
            # Стабильный порядок: старые лекции первые, по дате; затем по URL для устойчивости.
            ordered = sorted(
                group,
                key=lambda l: (u.parse_date(l.get("date", "")), l.get("url", "")),
            )
            for idx, lesson in enumerate(ordered, start=1):
                combined = f"{lesson.get('title', '')} (часть {idx})"
                stems[lesson["url"]] = expected_filename_stem(combined)
    return stems


def scan_disk() -> list[Path]:
    if not VIDEOS.exists():
        return []
    return [
        f for f in VIDEOS.rglob("*")
        if f.is_file() and f.suffix.lower() in VIDEO_EXTS
    ]


def plan_actions(lessons: list[dict[str, str]]) -> tuple[
    list[tuple[Path, Path, dict[str, str]]],
    list[dict[str, str]],
    list[tuple[Path, str]],
]:
    """Вернуть (rename_plan, download_plan, ambiguous_files).

    rename_plan: список (old_path, new_path, lesson)
    download_plan: список lessons для скачивания
    ambiguous_files: файлы с неоднозначным владельцем (несколько кандидатов)
    """
    stems = compute_expected_stems(lessons)

    expected_by_folder: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    lessons_by_folder_norm: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for lesson in lessons:
        sf = m.sanitize_folder_name(lesson.get("subject", ""))
        stem = stems[lesson["url"]]
        expected_by_folder[sf][stem] = lesson
        nt = m.normalize_media_name(lesson.get("title", ""))
        lessons_by_folder_norm[(sf, nt)].append(lesson)

    disk_files = scan_disk()

    # Pass 1: exact match
    claimed_lessons: set[str] = set()
    claimed_files: set[Path] = set()
    for f in disk_files:
        sf = f.parent.name
        if f.stem in expected_by_folder.get(sf, {}):
            lesson = expected_by_folder[sf][f.stem]
            claimed_lessons.add(lesson["url"])
            claimed_files.add(f)

    rename_plan: list[tuple[Path, Path, dict[str, str]]] = []
    ambiguous_files: list[tuple[Path, str]] = []

    # Pass 2: try to rename remaining files
    for f in disk_files:
        if f in claimed_files:
            continue
        sf = f.parent.name
        nt = m.normalize_media_name(f.stem)
        if not nt:
            continue

        candidates = [
            l for l in lessons_by_folder_norm.get((sf, nt), [])
            if l["url"] not in claimed_lessons
        ]

        if len(candidates) == 1:
            lesson = candidates[0]
            target = f.parent / f"{stems[lesson['url']]}{f.suffix}"
            if target.exists() and target != f:
                ambiguous_files.append((f, "target file already exists"))
                continue
            rename_plan.append((f, target, lesson))
            claimed_lessons.add(lesson["url"])
            claimed_files.add(f)
        elif len(candidates) > 1:
            ambiguous_files.append(
                (f, f"{len(candidates)} candidate lessons, manual review")
            )

    # Pass 3: find lessons without any disk file
    download_plan: list[dict[str, str]] = []
    for lesson in lessons:
        if lesson["url"] in claimed_lessons:
            continue
        sf = m.sanitize_folder_name(lesson.get("subject", ""))
        stem = stems[lesson["url"]]
        already_on_disk = False
        for ext in VIDEO_EXTS:
            if (VIDEOS / sf / f"{stem}{ext}").exists():
                already_on_disk = True
                break
        if not already_on_disk:
            download_plan.append(lesson)

    return rename_plan, download_plan, ambiguous_files


def remove_from_archive(vimeo_id: str) -> bool:
    if not ARCHIVE_FILE.exists() or not vimeo_id:
        return False
    lines = ARCHIVE_FILE.read_text(encoding="utf-8").splitlines()
    target = f"vimeo {vimeo_id}"
    new_lines = [l for l in lines if l.strip() != target]
    if len(new_lines) == len(lines):
        return False
    ARCHIVE_FILE.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate existing downloads to new naming scheme")
    parser.add_argument("--dry-run", action="store_true", help="Print plan only, no changes")
    parser.add_argument("--skip-downloads", action="store_true", help="Rename only, do not download")
    args = parser.parse_args()

    lessons = u.parse_existing_lessons(LESSONS_FILE)
    rename_plan, download_plan, ambiguous = plan_actions(lessons)

    print("=" * 70)
    print("RECONCILE PLAN")
    print("=" * 70)
    print(f"Files to rename     : {len(rename_plan)}")
    print(f"Lessons to download : {len(download_plan)}")
    print(f"Ambiguous files     : {len(ambiguous)}")
    print()

    if rename_plan:
        print("--- RENAMES ---")
        for old, new, lesson in rename_plan:
            print(f"  [{lesson['subject']}]")
            print(f"    OLD: {old.name}")
            print(f"    NEW: {new.name}")
        print()

    if download_plan:
        print("--- DOWNLOADS ---")
        for lesson in download_plan:
            print(f"  [{lesson['subject']}] {lesson['title']}  ({lesson['url']})")
        print()

    if ambiguous:
        print("--- AMBIGUOUS (manual review) ---")
        for f, reason in ambiguous:
            print(f"  {f.relative_to(ROOT)} — {reason}")
        print()

    if args.dry_run:
        print("Dry-run only. No changes made.")
        return

    # Execute renames
    renamed_ok = 0
    renamed_err: list[tuple[Path, Path, str]] = []
    for old, new, _lesson in rename_plan:
        try:
            old.rename(new)
            renamed_ok += 1
        except Exception as exc:  # noqa: BLE001
            renamed_err.append((old, new, str(exc)))

    print("=" * 70)
    print(f"Renamed: {renamed_ok}/{len(rename_plan)}")
    if renamed_err:
        print("Rename errors:")
        for old, new, err in renamed_err:
            print(f"  {old.name} -> {new.name}: {err}")
    print()

    if args.skip_downloads:
        return

    # Execute downloads
    from config import load_env, get_int, get_bool
    env = load_env()
    quality = get_int(env, "QUALITY", 1080)
    fast = get_bool(env, "FAST", False)
    downloaded_ok = 0
    downloaded_err: list[tuple[dict[str, str], str]] = []
    for idx, lesson in enumerate(download_plan, start=1):
        sf = m.sanitize_folder_name(lesson["subject"])
        target_dir = VIDEOS / sf
        vid_id = u.extract_vimeo_id(lesson["url"])
        # Clear archive entry so yt-dlp does not skip
        if vid_id:
            remove_from_archive(vid_id)

        # Pick title — with date suffix if collision
        stems = compute_expected_stems(lessons)
        # Reverse the stem to make filename — pass stem directly via lesson_title
        # But download() wraps lesson_title with _sanitize_filename, so reuse the stem
        # by passing it as title (it is already sanitized).
        title_for_file = stems[lesson["url"]]

        print(f"\n[{idx}/{len(download_plan)}] {lesson['subject']} :: {lesson['title']}")
        try:
            download(
                lesson["url"],
                quality=quality,
                output_dir=target_dir,
                fast=fast,
                lesson_title=title_for_file,
            )
            downloaded_ok += 1
        except Exception as exc:  # noqa: BLE001
            downloaded_err.append((lesson, str(exc)))
            print(f"  ERROR: {exc}")

    print("=" * 70)
    print("FINAL REPORT")
    print("=" * 70)
    print(f"Renamed   : {renamed_ok}/{len(rename_plan)}")
    print(f"Downloaded: {downloaded_ok}/{len(download_plan)}")
    print(f"Ambiguous : {len(ambiguous)}")
    if downloaded_err:
        print("\nDownload errors:")
        for lesson, err in downloaded_err:
            print(f"  [{lesson['subject']}] {lesson['title']}")
            print(f"    {err}")


if __name__ == "__main__":
    main()
