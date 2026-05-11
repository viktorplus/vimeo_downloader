"""Разовая синхронизация videos/ → внешнее хранилище.

Логика:
- Источник:   d:\\dev\\vimeo_downloader\\videos\\<subject>\\<file>.mp4
- Назначение: Z:\\videos\\<subject>\\<file>.mp4
- Если файл с таким же именем уже есть в назначении — пропускаем (не перезаписываем).
- Если в назначении есть файл с тем же нормализованным именем (другая орфография /
  старое имя по Vimeo-title) — тоже пропускаем (считаем что урок уже на месте).
- Иначе — копируем с правильным именем.

Запуск:   python sync_to_external.py [--dry-run] [--dest Z:\\videos]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "videos"
DEFAULT_DEST = Path(r"Z:\videos")

sys.path.insert(0, str(ROOT))
import lessons_menu as m  # noqa: E402

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".m4v"}


def human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync local videos/ to external storage")
    parser.add_argument("--dest", default=str(DEFAULT_DEST), help="Destination root (default: Z:\\videos)")
    parser.add_argument("--dry-run", action="store_true", help="Show plan only, no copying")
    args = parser.parse_args()

    dest_root = Path(args.dest)
    if not SOURCE.exists():
        print(f"ERROR: source folder does not exist: {SOURCE}")
        return 1
    if not dest_root.exists():
        print(f"ERROR: destination root does not exist: {dest_root}")
        return 1

    to_copy: list[tuple[Path, Path]] = []
    skipped_same_name: list[Path] = []
    skipped_similar: list[tuple[Path, Path]] = []

    # Cache normalized names per destination subject folder
    dest_norms: dict[str, dict[str, Path]] = {}

    for src_file in sorted(SOURCE.rglob("*")):
        if not src_file.is_file() or src_file.suffix.lower() not in VIDEO_EXTS:
            continue
        subject = src_file.parent.name
        dest_dir = dest_root / subject
        target = dest_dir / src_file.name

        if target.exists():
            skipped_same_name.append(src_file)
            continue

        # Build/cache normalized index for this destination subject folder
        if subject not in dest_norms:
            dest_norms[subject] = {}
            if dest_dir.exists():
                for z_f in dest_dir.iterdir():
                    if z_f.is_file() and z_f.suffix.lower() in VIDEO_EXTS:
                        dest_norms[subject][m.normalize_media_name(z_f.stem)] = z_f

        nt = m.normalize_media_name(src_file.stem)
        existing = dest_norms[subject].get(nt)
        if existing is not None:
            skipped_similar.append((src_file, existing))
            continue

        to_copy.append((src_file, target))

    total_bytes = sum(s.stat().st_size for s, _ in to_copy)

    print("=" * 70)
    print("SYNC PLAN")
    print("=" * 70)
    print(f"Source     : {SOURCE}")
    print(f"Destination: {dest_root}")
    print(f"To copy            : {len(to_copy)} ({human_size(total_bytes)})")
    print(f"Skipped (same name): {len(skipped_same_name)}")
    print(f"Skipped (similar)  : {len(skipped_similar)}")
    print()

    if skipped_similar:
        print("--- SKIPPED (file under different name already on destination) ---")
        for src, dst in skipped_similar:
            print(f"  [{src.parent.name}]")
            print(f"    LOCAL : {src.name}")
            print(f"    REMOTE: {dst.name}")
        print()

    if to_copy:
        print("--- TO COPY ---")
        for src, dst in to_copy:
            print(f"  [{src.parent.name}] {src.name}  ({human_size(src.stat().st_size)})")
        print()

    if args.dry_run:
        print("Dry-run only. No files copied.")
        return 0

    copied = 0
    failed: list[tuple[Path, str]] = []
    for src, dst in to_copy:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
            print(f"[{copied}/{len(to_copy)}] copied: {src.name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((src, str(exc)))
            print(f"FAILED: {src.name}: {exc}")

    print()
    print("=" * 70)
    print("REPORT")
    print("=" * 70)
    print(f"Copied : {copied}/{len(to_copy)}")
    print(f"Skipped: same-name {len(skipped_same_name)}, similar-name {len(skipped_similar)}")
    print(f"Failed : {len(failed)}")
    if failed:
        for src, err in failed:
            print(f"  {src.name}: {err}")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
