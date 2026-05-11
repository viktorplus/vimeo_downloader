#!/usr/bin/env python3
"""Локальное меню для скачивания уроков из lessons_list.txt."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
import re
import threading
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template_string, request

from download_lessons import download
from update_lessons_list import RECORDS_URL, update_lessons_file
from config import load_env, get_paths

QUALITIES = [360, 480, 720, 1080, 1440, 2160]
_ENV = load_env()
_VIDEOS_DIRS: list[Path] = get_paths(_ENV, "VIDEOS_DIRS", fallback=[Path("videos")])
DEFAULT_OUTPUT_ROOT = _VIDEOS_DIRS[0]
MIRROR_OUTPUT_ROOTS: list[Path] = _VIDEOS_DIRS[1:]
LESSONS_FILE = Path("lessons_list.txt")
ARCHIVE_FILE = Path(".downloaded")
DOWNLOADED_LESSONS_JSON = Path("downloaded_lessons.json")
DOWNLOADED_LESSONS_TXT = Path("downloaded_lessons.txt")
ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


def sanitize_folder_name(name: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]", "_", name).strip()
    value = re.sub(r"\s+", " ", value)
    return value or "Other"


def normalize_media_name(value: str) -> str:
  text = unicodedata.normalize("NFKC", (value or "").strip()).casefold()
  text = text.replace("_", " ")
  text = re.sub(r"^[*•·\-\s]+", "", text)
  text = re.sub(r"\s+", " ", text)
  return text.strip()


def clean_error_text(value: str) -> str:
    return ANSI_ESCAPE_RE.sub("", value or "")


def format_exception_message(exc: Exception) -> str:
    msg = clean_error_text(str(exc)).strip()
    if not msg:
        msg = clean_error_text(repr(exc)).strip()
    if not msg:
        msg = "Неизвестная ошибка (пустой текст исключения)"
    return f"{type(exc).__name__}: {msg}"


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


def lesson_key(url: str) -> str:
    vid = extract_vimeo_id(url)
    if vid:
        return f"vimeo:{vid}"
    return f"url:{(url or '').strip()}"


def load_downloaded_registry() -> dict[str, dict[str, str]]:
    if not DOWNLOADED_LESSONS_JSON.exists():
        return {}
    try:
        raw = json.loads(DOWNLOADED_LESSONS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def save_downloaded_registry(registry: dict[str, dict[str, str]]) -> None:
    DOWNLOADED_LESSONS_JSON.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    rows = sorted(
        registry.values(),
        key=lambda x: x.get("downloaded_at", ""),
        reverse=True,
    )
    lines = ["Скачанные уроки", "=" * 80, f"Всего: {len(rows)}", ""]
    for item in rows:
        lines.append(f"[{item.get('downloaded_at', '')}] {item.get('title', '')}")
        lines.append(f"  Предмет: {item.get('subject', '')}")
        lines.append(f"  Видео: {item.get('url', '')}")
        lines.append("")
    DOWNLOADED_LESSONS_TXT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def sync_registry_from_archive(registry: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    if not ARCHIVE_FILE.exists():
        return registry

    changed = False
    for line in ARCHIVE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("vimeo "):
            continue
        vid = line.split(" ", 1)[1].strip()
        key = f"vimeo:{vid}"
        if key in registry:
            continue
        registry[key] = {
            "downloaded_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            "title": f"Vimeo {vid}",
            "subject": "",
            "url": f"https://vimeo.com/{vid}",
        }
        changed = True

    if changed:
        save_downloaded_registry(registry)
    return registry


def sync_registry_from_existing_files(
    registry: dict[str, dict[str, str]],
    lessons: list[dict[str, str]],
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, dict[str, str]]:
    if not output_root.exists():
        return registry

    changed = False
    by_subject: dict[str, list[dict[str, str]]] = {}
    for lesson in lessons:
        by_subject.setdefault(sanitize_folder_name(lesson.get("subject", "")), []).append(lesson)

    video_exts = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
    for file_path in output_root.rglob("*"):
        if not file_path.is_file() or file_path.suffix.lower() not in video_exts:
            continue

        subject_name = file_path.parent.name
        candidates = by_subject.get(subject_name, [])
        if not candidates:
            continue

        file_name_norm = normalize_media_name(file_path.stem)
        if not file_name_norm:
            continue

        for lesson in candidates:
            key = lesson_key(lesson.get("url", ""))
            if key in registry:
                continue

            title_norm = normalize_media_name(lesson.get("title", ""))
            if not title_norm:
                continue

            if file_name_norm == title_norm:
                registry[key] = {
                    "downloaded_at": datetime.fromtimestamp(file_path.stat().st_mtime).strftime("%d.%m.%Y %H:%M:%S"),
                    "title": lesson.get("title", ""),
                    "subject": lesson.get("subject", ""),
                    "url": lesson.get("url", ""),
                }
                changed = True
                break

    if changed:
        save_downloaded_registry(registry)
    return registry


def mark_lesson_downloaded(lesson: dict[str, str]) -> None:
    key = lesson_key(lesson.get("url", ""))
    if not key:
        return
    with downloaded_lock:
        registry = load_downloaded_registry()
        registry = sync_registry_from_archive(registry)
        registry[key] = {
            "downloaded_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
            "title": lesson.get("title", ""),
            "subject": lesson.get("subject", ""),
            "url": lesson.get("url", ""),
        }
        save_downloaded_registry(registry)


def ensure_downloaded_registry_files() -> None:
  with downloaded_lock:
    registry = load_downloaded_registry()
    registry = sync_registry_from_archive(registry)
    registry = sync_registry_from_existing_files(registry, lessons_cache)
    if not DOWNLOADED_LESSONS_JSON.exists() or not DOWNLOADED_LESSONS_TXT.exists():
      save_downloaded_registry(registry)


def parse_lessons(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")

    lessons: list[dict[str, str]] = []
    current_subject = "Unknown"

    lesson_re = re.compile(r"^\s*\[(?P<date>[^\]]+)\]\s*(?P<title>.+)$")
    subject_re = re.compile(r"^\s{2}(?P<subject>.+?)\s+\(\d+\s+уроков\)\s*$")

    lines = path.read_text(encoding="utf-8").splitlines()
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
                lessons.append(
                    {
                        "id": str(len(lessons)),
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

    def parse_lesson_date(value: str) -> datetime:
      try:
        return datetime.strptime(value.strip(), "%d.%m.%Y")
      except ValueError:
        return datetime.min

    # Always show newest lessons first in the UI.
    lessons.sort(key=lambda x: (parse_lesson_date(x["date"]), x["title"].lower()), reverse=True)
    for idx, lesson in enumerate(lessons):
      lesson["id"] = str(idx)

    return lessons


app = Flask(__name__)
lessons_cache: list[dict[str, str]] = parse_lessons(LESSONS_FILE)
lessons_lock = threading.Lock()
recently_added_keys: set[str] = set()
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()
downloaded_lock = threading.Lock()
sync_state_lock = threading.Lock()
sync_state: dict[str, str] = {
  "status": "idle",
  "message": "Нажмите «Обновить список из LMS», если появились новые видео.",
  "updated_at": "",
}
ensure_downloaded_registry_files()


def run_download_job(job_id: str, lesson: dict[str, str], quality: int, output_root: str, fast: bool = False) -> None:
    try:
        output_dir = Path(output_root) / sanitize_folder_name(lesson["subject"])
        with jobs_lock:
            jobs[job_id]["status"] = "running"
            jobs[job_id]["output_dir"] = str(output_dir.resolve())

        subj = sanitize_folder_name(lesson["subject"])
        mirror_dirs = [root / subj for root in MIRROR_OUTPUT_ROOTS]
        download_result = download(
            lesson["url"],
            quality,
            output_dir,
            fast=fast,
            lesson_title=lesson.get("title"),
            extra_output_dirs=mirror_dirs,
        )

        with jobs_lock:
            jobs[job_id]["status"] = "done"
            if download_result:
                jobs[job_id]["message"] = "Скачивание завершено"
            else:
                jobs[job_id]["message"] = "Уже был скачан ранее — пропущено"
        # Even when skipped, mark it in local registry so UI shows "already downloaded".
        mark_lesson_downloaded(lesson)
    except Exception as exc:  # noqa: BLE001
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["message"] = format_exception_message(exc)


def run_batch_download_job(
    job_id: str,
    lessons: list[dict[str, str]],
    quality: int,
    output_root: str,
    fast: bool = False,
) -> None:
    total = len(lessons)
    with jobs_lock:
        jobs[job_id]["status"] = "running"
        jobs[job_id]["total"] = total
        jobs[job_id]["completed"] = 0

    downloaded = 0
    skipped = 0
    try:
        for idx, lesson in enumerate(lessons, start=1):
            output_dir = Path(output_root) / sanitize_folder_name(lesson["subject"])
            with jobs_lock:
                jobs[job_id]["current_lesson"] = lesson["title"]
                jobs[job_id]["output_dir"] = str(output_dir.resolve())

            subj = sanitize_folder_name(lesson["subject"])
            mirror_dirs = [root / subj for root in MIRROR_OUTPUT_ROOTS]
            result = download(
                lesson["url"],
                quality,
                output_dir,
                fast=fast,
                lesson_title=lesson.get("title"),
                extra_output_dirs=mirror_dirs,
            )
            if result:
                downloaded += 1
            else:
                skipped += 1
            mark_lesson_downloaded(lesson)

            with jobs_lock:
                jobs[job_id]["completed"] = idx

        with jobs_lock:
            jobs[job_id]["status"] = "done"
            parts = [f"Скачано: {downloaded}"]
            if skipped:
                parts.append(f"пропущено (уже было): {skipped}")
            jobs[job_id]["message"] = ", ".join(parts)
    except Exception as exc:  # noqa: BLE001
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["message"] = format_exception_message(exc)


def run_sync_lessons_job(job_id: str, records_url: str) -> None:
  global lessons_cache, recently_added_keys

  try:
    with jobs_lock:
      jobs[job_id]["status"] = "running"
      jobs[job_id]["message"] = "Откроется браузер LMS. Войдите в кабинет, если потребуется."
    with sync_state_lock:
      sync_state["status"] = "running"
      sync_state["message"] = "Синхронизация выполняется..."
      sync_state["updated_at"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    def on_progress(message: str) -> None:
      print(f"[SYNC] {message}", flush=True)
      with jobs_lock:
        jobs[job_id]["message"] = message
      with sync_state_lock:
        sync_state["status"] = "running"
        sync_state["message"] = message
        sync_state["updated_at"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    with lessons_lock:
      before_keys = {lesson_key(x.get("url", "")) for x in lessons_cache}

    stats = update_lessons_file(
      output=LESSONS_FILE,
      records_url=records_url,
      prompt_for_enter=False,
      timeout_sec=900,
      progress_cb=on_progress,
    )
    reloaded = parse_lessons(LESSONS_FILE)
    after_keys = {lesson_key(x.get("url", "")) for x in reloaded}
    with lessons_lock:
      lessons_cache = reloaded
      recently_added_keys = {k for k in after_keys if k and k not in before_keys}

    done_message = (
      f"Готово. Найдено: {stats['found']}, добавлено: {stats['added']}, "
      f"всего: {stats['total']}"
    )
    with jobs_lock:
      jobs[job_id]["status"] = "done"
      jobs[job_id]["message"] = done_message
    with sync_state_lock:
      sync_state["status"] = "done"
      sync_state["message"] = done_message
      sync_state["updated_at"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
  except Exception as exc:  # noqa: BLE001
    err = format_exception_message(exc)
    with jobs_lock:
      jobs[job_id]["status"] = "error"
      jobs[job_id]["message"] = err
    with sync_state_lock:
      sync_state["status"] = "error"
      sync_state["message"] = err
      sync_state["updated_at"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")


TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Меню скачивания уроков</title>
  <style>
    :root {
      --bg: #f4f6f8;
      --card: #ffffff;
      --text: #1f2a37;
      --muted: #5b6775;
      --line: #dbe2ea;
      --accent: #0f766e;
      --accent-2: #115e59;
      --err: #b42318;
    }
    body {
      margin: 0;
      font-family: "Segoe UI", "Helvetica Neue", sans-serif;
      color: var(--text);
      background: linear-gradient(180deg, #f7fafc, #eef3f7);
    }
    .wrap {
      max-width: 1200px;
      margin: 24px auto;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: 0 10px 35px rgba(16, 24, 40, 0.08);
      overflow: hidden;
    }
    .head {
      padding: 18px 20px;
      background: linear-gradient(90deg, #0f766e, #0f766e 40%, #115e59);
      color: #fff;
    }
    .head h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 700;
    }
    .head p {
      margin: 8px 0 0;
      opacity: 0.9;
      font-size: 14px;
    }
    .toolbar {
      display: grid;
      grid-template-columns: 1fr 200px 240px 230px;
      gap: 10px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      background: #fbfdff;
    }
    .syncbar {
      display: flex;
      gap: 10px;
      align-items: center;
      padding: 0 16px 14px;
      border-bottom: 1px solid var(--line);
      background: #fbfdff;
    }
    .bulkbar {
      display: grid;
      grid-template-columns: 1fr 120px 240px;
      gap: 10px;
      padding: 0 16px 14px;
      border-bottom: 1px solid var(--line);
      background: #fbfdff;
      align-items: center;
    }
    .bulk-title {
      color: #475467;
      font-size: 13px;
    }
    .toolbar input, .toolbar select {
      border: 1px solid #c8d3df;
      border-radius: 10px;
      padding: 10px 12px;
      font-size: 14px;
      width: 100%;
      box-sizing: border-box;
    }
    .bulkbar select {
      border: 1px solid #c8d3df;
      border-radius: 10px;
      padding: 10px 12px;
      font-size: 14px;
      width: 100%;
      box-sizing: border-box;
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    th, td {
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      text-align: left;
      font-size: 14px;
    }
    th {
      background: #f8fbfd;
      color: #4b5563;
      position: sticky;
      top: 0;
      z-index: 1;
    }
    .title {
      font-weight: 600;
      color: #0b1220;
    }
    .meta {
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
    }
    .quality {
      width: 90px;
      border: 1px solid #c8d3df;
      border-radius: 8px;
      padding: 6px 8px;
      font-size: 13px;
    }
    .btn {
      background: var(--accent);
      color: white;
      border: 0;
      border-radius: 8px;
      padding: 7px 10px;
      cursor: pointer;
      font-weight: 600;
    }
    .btn:hover {
      background: var(--accent-2);
    }
    .status {
      font-size: 12px;
      margin-top: 4px;
      color: #334155;
      min-height: 16px;
    }
    .status.error {
      color: var(--err);
    }
    .badge {
      display: inline-block;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 11px;
      font-weight: 700;
      margin-left: 8px;
      vertical-align: middle;
    }
    .badge.ok {
      background: #dcfce7;
      color: #166534;
    }
    .badge.new {
      background: #dbeafe;
      color: #1d4ed8;
    }
    .btn[disabled] {
      background: #9aa4b2;
      cursor: not-allowed;
    }
    #bulkStatus {
      font-size: 13px;
      color: #334155;
      min-height: 18px;
    }
    #syncStatus {
      font-size: 13px;
      color: #334155;
      min-height: 18px;
    }
    #bulkStatus.error {
      color: var(--err);
    }
    #syncStatus.error {
      color: var(--err);
    }
    @media (max-width: 900px) {
      .toolbar {
        grid-template-columns: 1fr;
      }
      .bulkbar {
        grid-template-columns: 1fr;
      }
      table {
        font-size: 13px;
      }
      th:nth-child(4), td:nth-child(4) {
        display: none;
      }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="head">
      <h1>Скачивание уроков</h1>
      <p>Файлы сохраняются локально в папку <b id="folderHint">videos/ПРЕДМЕТ</b>.</p>
    </div>

    <div class="toolbar">
      <input id="search" placeholder="Поиск по названию урока" />
      <select id="subjectFilter">
        <option value="">Все предметы</option>
        {% for subject in subjects %}
          <option value="{{ subject }}">{{ subject }}</option>
        {% endfor %}
      </select>
      <input id="outputRoot" value="{{ default_output }}" placeholder="Папка сохранения" />
      <button class="btn" id="syncBtn" onclick="startSyncLessons()">Обновить список из LMS</button>
    </div>

    <div class="syncbar">
      <div id="syncStatus" class="{% if sync_status == 'error' %}error{% endif %}">{{ sync_message }}</div>
      <div class="meta" id="syncMeta">{{ sync_updated_at and ('Последняя проверка: ' + sync_updated_at) or '' }}</div>
      <div class="meta">Скачано: {{ downloaded_count }}{% if new_count %} • Новых после синхронизации: {{ new_count }}{% endif %}</div>
    </div>

    <div class="bulkbar">
      <div class="bulk-title">Скачать все видимые (с учетом фильтра и поиска)</div>
      <select id="bulkQuality">
        {% for q in qualities %}
          <option value="{{ q }}" {% if q == 1080 %}selected{% endif %}>{{ q }}p</option>
        {% endfor %}
        <option value="720f">720p ⚡ быстро</option>
      </select>
      <button class="btn" id="bulkBtn" onclick="startBulkDownload()">Скачать отфильтрованные</button>
      <div id="bulkStatus"></div>
    </div>

    <table>
      <thead>
        <tr>
          <th style="width: 220px;">Дата / Предмет</th>
          <th>Урок</th>
          <th style="width: 150px;">Качество</th>
          <th style="width: 180px;">Действие</th>
        </tr>
      </thead>
      <tbody id="rows">
      {% for lesson in lessons %}
        <tr data-title="{{ lesson.title|lower }}" data-subject="{{ lesson.subject }}">
          <td>
            <div>{{ lesson.date }}</div>
            <div class="meta">{{ lesson.subject }}</div>
          </td>
          <td>
            <div class="title">{{ lesson.title }}</div>
            <div class="meta">Преподаватель: {{ lesson.teacher or "—" }}</div>
            {% if lesson.is_downloaded %}<span class="badge ok">Уже скачан</span>{% endif %}
            {% if lesson.is_new %}<span class="badge new">Новый</span>{% endif %}
          </td>
          <td>
            <select class="quality" id="quality-{{ lesson.id }}">
              {% for q in qualities %}
                <option value="{{ q }}" {% if q == 1080 %}selected{% endif %}>{{ q }}p</option>
              {% endfor %}
              <option value="720f">720p ⚡ быстро</option>
            </select>
          </td>
          <td>
            {% if lesson.is_downloaded %}
              <button class="btn" disabled>Уже скачан</button>
            {% else %}
              <button class="btn" onclick="startDownload('{{ lesson.id }}')">Скачать</button>
            {% endif %}
            <div id="status-{{ lesson.id }}" class="status"></div>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <script>
    const search = document.getElementById("search");
    const subjectFilter = document.getElementById("subjectFilter");
    const outputRoot = document.getElementById("outputRoot");
    const folderHint = document.getElementById("folderHint");
    const bulkStatus = document.getElementById("bulkStatus");
    const bulkBtn = document.getElementById("bulkBtn");
    const syncStatus = document.getElementById("syncStatus");
    const syncBtn = document.getElementById("syncBtn");

    function applyFilter() {
      const q = search.value.toLowerCase().trim();
      const s = subjectFilter.value;
      document.querySelectorAll("#rows tr").forEach((row) => {
        const titleOk = !q || row.dataset.title.includes(q);
        const subjectOk = !s || row.dataset.subject === s;
        row.style.display = titleOk && subjectOk ? "" : "none";
      });
    }

    search.addEventListener("input", applyFilter);
    subjectFilter.addEventListener("change", applyFilter);

    outputRoot.addEventListener("input", () => {
      const val = outputRoot.value.trim() || "videos";
      folderHint.textContent = val + "/ПРЕДМЕТ";
    });

    async function startDownload(lessonId) {
      const quality = document.getElementById(`quality-${lessonId}`).value;
      const status = document.getElementById(`status-${lessonId}`);
      status.classList.remove("error");
      status.textContent = "Запуск...";

      const payload = new URLSearchParams();
      payload.set("lesson_id", lessonId);
      payload.set("quality", quality);
      payload.set("output_root", outputRoot.value.trim() || "videos");

      const resp = await fetch("/download", {
        method: "POST",
        headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: payload.toString(),
      });

      const data = await resp.json();
      if (!resp.ok) {
        status.classList.add("error");
        status.textContent = data.error || "Ошибка";
        return;
      }

      status.textContent = "Скачивание запущено...";
      pollJob(data.job_id, status);
    }

    async function pollJob(jobId, statusEl) {
      let attempts = 0;
      while (attempts < 720) {
        await new Promise((r) => setTimeout(r, 1000));
        const resp = await fetch(`/job/${jobId}`);
        const data = await resp.json();

        if (data.status === "done") {
          statusEl.textContent = `Готово: ${data.output_dir}`;
          return;
        }

        if (data.status === "error") {
          statusEl.classList.add("error");
          statusEl.textContent = `Ошибка: ${data.message || "неизвестно"}`;
          return;
        }

        statusEl.textContent = "Скачивается...";
        attempts += 1;
      }

      statusEl.textContent = "Скачивание продолжается в фоне...";
    }

    function getVisibleLessonIds() {
      const ids = [];
      document.querySelectorAll("#rows tr").forEach((row) => {
        if (row.style.display === "none") return;
        const btn = row.querySelector("button.btn");
        if (!btn) return;
        const onclick = btn.getAttribute("onclick");
        if (!onclick) return; // already-downloaded rows have no onclick
        const match = onclick.match(/'([^']+)'/);
        if (match) ids.push(match[1]);
      });
      return ids;
    }

    async function startBulkDownload() {
      try {
        const lessonIds = getVisibleLessonIds();
        if (!lessonIds.length) {
          bulkStatus.classList.add("error");
          bulkStatus.textContent = "Нет видимых НЕскачанных уроков для скачивания";
          return;
        }

        bulkStatus.classList.remove("error");
        bulkStatus.textContent = `Запуск: ${lessonIds.length} уроков...`;
        bulkBtn.disabled = true;

        const payload = new URLSearchParams();
        payload.set("lesson_ids", lessonIds.join(","));
        payload.set("quality", document.getElementById("bulkQuality").value);
        payload.set("output_root", outputRoot.value.trim() || "videos");

        const resp = await fetch("/download-filtered", {
          method: "POST",
          headers: {"Content-Type": "application/x-www-form-urlencoded"},
          body: payload.toString(),
        });

        const data = await resp.json();
        if (!resp.ok) {
          bulkStatus.classList.add("error");
          bulkStatus.textContent = data.error || "Ошибка";
          bulkBtn.disabled = false;
          return;
        }

        pollBulkJob(data.job_id);
      } catch (err) {
        bulkStatus.classList.add("error");
        bulkStatus.textContent = `Ошибка запуска: ${err?.message || "неизвестно"}`;
        bulkBtn.disabled = false;
      }
    }

    async function startSyncLessons() {
      syncStatus.classList.remove("error");
      syncStatus.textContent = "Запуск синхронизации...";
      syncBtn.disabled = true;

      const resp = await fetch("/sync-lessons", {
        method: "POST",
        headers: {"Content-Type": "application/x-www-form-urlencoded"},
        body: "",
      });

      const data = await resp.json();
      if (!resp.ok) {
        syncStatus.classList.add("error");
        syncStatus.textContent = data.error || "Ошибка запуска синхронизации";
        syncBtn.disabled = false;
        return;
      }

      pollSyncJob(data.job_id);
    }

    async function pollSyncJob(jobId) {
      let attempts = 0;
      while (attempts < 1200) {
        await new Promise((r) => setTimeout(r, 1000));
        const resp = await fetch(`/job/${jobId}`);
        const data = await resp.json();

        if (data.status === "done") {
          syncStatus.textContent = data.message || "Список обновлен";
          syncBtn.disabled = false;
          setTimeout(() => location.reload(), 1200);
          return;
        }

        if (data.status === "error") {
          syncStatus.classList.add("error");
          syncStatus.textContent = `Ошибка: ${data.message || "неизвестно"}`;
          syncBtn.disabled = false;
          return;
        }

        syncStatus.textContent = data.message || "Синхронизация выполняется...";
        attempts += 1;
      }

      syncStatus.textContent = "Синхронизация продолжается в фоне...";
      syncBtn.disabled = false;
    }

    async function pollBulkJob(jobId) {
      let attempts = 0;
      while (attempts < 7200) {
        await new Promise((r) => setTimeout(r, 1000));
        const resp = await fetch(`/job/${jobId}`);
        const data = await resp.json();

        if (data.status === "done") {
          bulkStatus.textContent = `Готово: ${data.message}`;
          bulkBtn.disabled = false;
          return;
        }

        if (data.status === "error") {
          bulkStatus.classList.add("error");
          bulkStatus.textContent = `Ошибка: ${data.message || "неизвестно"}`;
          bulkBtn.disabled = false;
          return;
        }

        const completed = data.completed || 0;
        const total = data.total || 0;
        const lesson = data.current_lesson || "";
        bulkStatus.textContent = `Скачивается: ${completed}/${total}${lesson ? " • " + lesson : ""}`;
        attempts += 1;
      }

      bulkStatus.textContent = "Пакетное скачивание продолжается в фоне...";
      bulkBtn.disabled = false;
    }
  </script>
</body>
</html>
"""


@app.get("/")
def index() -> str:
    with lessons_lock:
        current_lessons = list(lessons_cache)
        current_new = set(recently_added_keys)

    with downloaded_lock:
        registry = load_downloaded_registry()
        registry = sync_registry_from_archive(registry)
        registry = sync_registry_from_existing_files(registry, current_lessons)
        downloaded_keys = set(registry.keys())

    prepared_lessons: list[dict[str, str | bool]] = []
    for lesson in current_lessons:
        key = lesson_key(lesson.get("url", ""))
        prepared = dict(lesson)
        prepared["is_downloaded"] = key in downloaded_keys
        prepared["is_new"] = key in current_new
        prepared_lessons.append(prepared)

    with sync_state_lock:
        current_sync = dict(sync_state)

    subjects = sorted({lesson["subject"] for lesson in prepared_lessons})
    return render_template_string(
        TEMPLATE,
        lessons=prepared_lessons,
        subjects=subjects,
        qualities=QUALITIES,
        downloaded_count=len(downloaded_keys),
        new_count=len(current_new),
        sync_status=current_sync["status"],
        sync_message=current_sync["message"],
        sync_updated_at=current_sync["updated_at"],
        default_output=str(DEFAULT_OUTPUT_ROOT),
    )


@app.post("/download")
def start_download():
    lesson_id = request.form.get("lesson_id", "")
    quality_raw = request.form.get("quality", "1080")
    output_root = request.form.get("output_root", str(DEFAULT_OUTPUT_ROOT)).strip()

    fast = quality_raw.endswith("f")
    quality_num = quality_raw.rstrip("f")
    try:
        quality = int(quality_num)
    except ValueError:
        return jsonify({"error": "Неверное качество"}), 400

    if quality not in QUALITIES:
        return jsonify({"error": "Качество не поддерживается"}), 400

    with lessons_lock:
      lesson = next((x for x in lessons_cache if x["id"] == lesson_id), None)
    if lesson is None:
        return jsonify({"error": "Урок не найден"}), 404

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "message": "",
            "lesson": lesson["title"],
            "output_dir": "",
        }

    th = threading.Thread(
        target=run_download_job,
        args=(job_id, lesson, quality, output_root),
        kwargs={"fast": fast},
        daemon=True,
    )
    th.start()

    return jsonify({"job_id": job_id})


@app.post("/download-filtered")
def start_filtered_download():
    ids_raw = request.form.get("lesson_ids", "").strip()
    quality_raw = request.form.get("quality", "1080")
    output_root = request.form.get("output_root", str(DEFAULT_OUTPUT_ROOT)).strip()

    fast = quality_raw.endswith("f")
    quality_num = quality_raw.rstrip("f")
    try:
        quality = int(quality_num)
    except ValueError:
        return jsonify({"error": "Неверное качество"}), 400

    if quality not in QUALITIES:
        return jsonify({"error": "Качество не поддерживается"}), 400

    if not ids_raw:
        return jsonify({"error": "Не переданы уроки"}), 400

    ids = [x for x in ids_raw.split(",") if x]
    with lessons_lock:
        selected = [lesson for lesson in lessons_cache if lesson["id"] in ids]
    if not selected:
        return jsonify({"error": "Нет уроков для скачивания"}), 400

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "message": "",
            "output_dir": "",
            "total": len(selected),
            "completed": 0,
            "current_lesson": "",
        }

    th = threading.Thread(
        target=run_batch_download_job,
        args=(job_id, selected, quality, output_root),
        kwargs={"fast": fast},
        daemon=True,
    )
    th.start()

    return jsonify({"job_id": job_id, "count": len(selected)})



@app.post("/sync-lessons")
def sync_lessons():
    records_url = request.form.get("records_url", RECORDS_URL).strip() or RECORDS_URL

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "message": "",
            "task": "sync_lessons",
        }

    th = threading.Thread(
        target=run_sync_lessons_job,
        args=(job_id, records_url),
        daemon=True,
    )
    th.start()

    return jsonify({"job_id": job_id})


@app.get("/job/<job_id>")
def get_job(job_id: str):
    with jobs_lock:
        info = jobs.get(job_id)
    if not info:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(info)


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальное меню скачивания уроков")
    parser.add_argument("--host", default="127.0.0.1", help="Host (по умолчанию: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5050, help="Port (по умолчанию: 5050)")
    args = parser.parse_args()

    print(f"Откройте в браузере: http://{args.host}:{args.port}")
    print(f"Видео будут сохраняться в: {DEFAULT_OUTPUT_ROOT.resolve()}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()


