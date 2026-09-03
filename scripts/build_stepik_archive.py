#!/usr/bin/env python3
"""Build a static, navigable archive from Stepik pages exported via Browser."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import re
import shutil
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


COURSE_SLUG = "figma-ux-editors"
COURSE_SOURCE = "https://stepik.org/course/286605"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def absolute_url(value: str, base: str) -> str:
    if not value or value.startswith(("data:", "blob:", "#")):
        return value
    return urllib.parse.urljoin(base, html.unescape(value))


def safe_asset_name(url: str, content_type: str = "") -> str:
    parsed = urllib.parse.urlparse(url)
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(parsed.path).name).strip("-.")
    if not stem:
        stem = "asset"
    suffix = Path(stem).suffix.lower()
    if not suffix:
        suffix = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".bin"
        stem += suffix
    digest = hashlib.sha256(url.encode()).hexdigest()[:10]
    return f"{Path(stem).stem[:70]}-{digest}{Path(stem).suffix.lower()}"


def collect_asset_urls(manifest: dict, pages: list[dict]) -> dict[str, str]:
    originals: dict[str, str] = {}

    def add(value: str, base: str):
        if not value or value.startswith(("data:", "blob:")):
            return
        absolute = absolute_url(value, base)
        if absolute.startswith(("http://", "https://")):
            originals[value] = absolute
            originals[absolute] = absolute

    add(manifest["course"].get("cover", ""), COURSE_SOURCE)
    attr_re = re.compile(r"(?:src|data-src|poster)\s*=\s*([\"'])(.*?)\1", re.I | re.S)
    for page in pages:
        base = page["url"]
        for image in page.get("images", []):
            add(image.get("src", ""), base)
        for video in page.get("videos", []):
            add(video.get("poster", ""), base)
        for _, value in attr_re.findall(page.get("html", "")):
            if re.search(r"\.(?:png|jpe?g|gif|webp|svg)(?:[?#]|$)", value, re.I):
                add(value, base)
    return originals


def download_assets(urls: dict[str, str], assets_dir: Path) -> tuple[dict[str, str], list[str]]:
    assets_dir.mkdir(parents=True, exist_ok=True)
    unique = sorted(set(urls.values()))
    downloaded: dict[str, str] = {}
    failures: list[str] = []

    def download(url: str):
        last_error = None
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (course archive)"})
                with urllib.request.urlopen(request, timeout=60) as response:
                    data = response.read()
                    content_type = response.headers.get("Content-Type", "")
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        else:
            raise last_error
        name = safe_asset_name(url, content_type)
        (assets_dir / name).write_bytes(data)
        return url, name

    with ThreadPoolExecutor(max_workers=10) as pool:
        future_map = {pool.submit(download, url): url for url in unique}
        for future in as_completed(future_map):
            url = future_map[future]
            try:
                source, name = future.result()
                downloaded[source] = name
            except Exception as exc:  # keep the remote URL when a CDN asset cannot be copied
                failures.append(f"{url}: {exc}")

    mapping: dict[str, str] = {}
    for original, absolute in urls.items():
        if absolute in downloaded:
            mapping[original] = downloaded[absolute]
            mapping[absolute] = downloaded[absolute]
    return mapping, failures


def clean_content(raw: str, source_url: str, asset_map: dict[str, str]) -> str:
    content = raw or ""
    content = re.sub(r"<script\b[^>]*>.*?</script>", "", content, flags=re.I | re.S)
    content = re.sub(r"\s+on[a-z]+\s*=\s*([\"']).*?\1", "", content, flags=re.I | re.S)
    content = re.sub(r"\s+srcset\s*=\s*([\"']).*?\1", "", content, flags=re.I | re.S)

    for original in sorted(asset_map, key=len, reverse=True):
        content = content.replace(original, f"../assets/{asset_map[original]}")
        content = content.replace(html.escape(original, quote=True), f"../assets/{asset_map[original]}")

    def absolutize(match: re.Match) -> str:
        quote, value = match.group(1), match.group(2)
        if value.startswith(("../assets/", "data:", "#", "mailto:", "tel:")):
            return f"href={quote}{value}{quote}"
        return f"href={quote}{absolute_url(value, source_url)}{quote}"

    content = re.sub(r"href\s*=\s*([\"'])(.*?)\1", absolutize, content, flags=re.I | re.S)
    content = re.sub(r"<img\b(?![^>]*\bloading=)", '<img loading="lazy" ', content, flags=re.I)
    return content


def best_video(video: dict) -> str:
    sources = video.get("sources") or []
    preferred = sorted(
        (s for s in sources if s.get("src")),
        key=lambda s: abs((int(s.get("res") or 0) if str(s.get("res") or "").isdigit() else 0) - 720),
    )
    return (preferred[0]["src"] if preferred else video.get("src", "")) or ""


def navigation_html(page: dict, pages: list[dict], current_index: int) -> str:
    sections: dict[int, dict] = {}
    for index, item in enumerate(pages):
        section = sections.setdefault(item["section_position"], {"title": item["section_title"], "lessons": {}})
        lesson = section["lessons"].setdefault(
            item["lesson_id"],
            {"position": item["position"], "title": item["title"].strip(), "pages": []},
        )
        lesson["pages"].append((index, item))

    chunks = []
    for section_number, section in sections.items():
        lesson_chunks = []
        for lesson in section["lessons"].values():
            steps = []
            for index, item in lesson["pages"]:
                active = ' aria-current="page" class="active"' if index == current_index else ""
                steps.append(
                    f'<a{active} href="{page_filename(item)}"><span>{item["step_position"]}</span></a>'
                )
            lesson_chunks.append(
                '<li><div class="lesson-row">'
                f'<a class="lesson-link" href="{page_filename(lesson["pages"][0][1])}">'
                f'<span>{section_number}.{lesson["position"]}</span>{html.escape(lesson["title"])}</a>'
                f'<div class="step-dots">{"".join(steps)}</div></div></li>'
            )
        is_open = " open" if section_number == page["section_position"] else ""
        chunks.append(
            f'<details{is_open}><summary><span>{section_number:02d}</span>{html.escape(section["title"].strip())}</summary>'
            f'<ol>{"".join(lesson_chunks)}</ol></details>'
        )
    return "".join(chunks)


def page_filename(page: dict) -> str:
    return f'{page["section_position"]:02d}-{page["position"]:02d}-{page["step_position"]:02d}.html'


def plural_steps(value: int) -> str:
    if value % 10 == 1 and value % 100 != 11:
        return "шаг"
    if value % 10 in (2, 3, 4) and value % 100 not in (12, 13, 14):
        return "шага"
    return "шагов"


def render_step(page: dict, pages: list[dict], index: int, asset_map: dict[str, str]) -> str:
    previous = pages[index - 1] if index else None
    following = pages[index + 1] if index + 1 < len(pages) else None
    heading = page.get("heading") or f'Шаг {page["step_position"]}'
    body = "" if page.get("videos") else clean_content(page.get("html", ""), page["url"], asset_map)

    video_html = ""
    if page.get("videos"):
        video = page["videos"][0]
        source = best_video(video)
        poster = video.get("poster", "")
        poster_value = f' poster="../assets/{asset_map[poster]}"' if poster in asset_map else (
            f' poster="{html.escape(poster, quote=True)}"' if poster else ""
        )
        video_html = (
            f'<div class="video-frame"><video controls preload="metadata"{poster_value}>'
            f'<source src="{html.escape(source, quote=True)}" type="video/mp4"></video></div>'
        )

    if not body and not video_html:
        body = '<p class="empty-note">На этом шаге нет текстового содержимого. Используйте ссылку на оригинал Stepik.</p>'

    back = f'<a href="{page_filename(previous)}">← Предыдущий</a>' if previous else '<span></span>'
    next_link = f'<a href="{page_filename(following)}">Следующий →</a>' if following else '<a href="../index.html">К оглавлению ↑</a>'
    quiz_note = '<p class="archive-note">Это сохранённая копия задания. Ответы в ней не отправляются в Stepik.</p>' if "material" in page.get("kind", "") and re.search(r"<(?:input|textarea|select|button)\b", body, re.I) else ""

    return f'''<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(page["title"].strip())} · шаг {page["step_position"]}</title>
  <meta name="description" content="Локальная копия урока курса «Фигма для UX-редакторов».">
  <link rel="icon" type="image/png" sizes="512x512" href="../../../assets/favicon.png">
  <link rel="apple-touch-icon" href="../../../assets/favicon.png">
  <link rel="stylesheet" href="../styles.css">
</head>
<body class="step-page">
  <header class="topbar"><a href="../index.html">Figma для UX‑редакторов</a><span>{index + 1} / {len(pages)}</span><a href="../../../index.html">Все курсы</a></header>
  <div class="course-shell">
    <aside class="course-nav"><a class="course-home" href="../index.html">Оглавление курса</a>{navigation_html(page, pages, index)}</aside>
    <main class="step-main">
      <div class="breadcrumb"><span>{page["section_position"]:02d}</span><span>{html.escape(page["section_title"].strip())}</span></div>
      <p class="lesson-label">Урок {page["section_position"]}.{page["position"]} · шаг {page["step_position"]}</p>
      <h1>{html.escape(heading)}</h1>
      <div class="source-actions"><a href="{html.escape(page["url"], quote=True)}" target="_blank" rel="noopener">Открыть оригинал на Stepik ↗</a></div>
      {quiz_note}{video_html}<article class="rich-content">{body}</article>
      <nav class="page-nav">{back}{next_link}</nav>
    </main>
  </div>
</body>
</html>
'''


def render_course_index(manifest: dict, pages: list[dict], asset_map: dict[str, str]) -> str:
    course = manifest["course"]
    cover = course.get("cover", "")
    cover_src = f'assets/{asset_map[cover]}' if cover in asset_map else cover
    section_blocks = []
    pages_by_lesson: dict[int, list[dict]] = {}
    for page in pages:
        pages_by_lesson.setdefault(page["lesson_id"], []).append(page)

    for section in manifest["sections"]:
        lessons = []
        for lesson in section["lessons"]:
            lesson_pages = pages_by_lesson.get(lesson["lesson_id"], [])
            if not lesson_pages:
                continue
            step_links = "".join(
                f'<a href="steps/{page_filename(page)}">{page["step_position"]}</a>' for page in lesson_pages
            )
            lessons.append(
                '<li><a class="index-lesson" href="steps/' + page_filename(lesson_pages[0]) + '">'
                f'<span>{section["position"]}.{lesson["position"]}</span><strong>{html.escape(lesson["title"].strip())}</strong>'
                f'<small>{len(lesson_pages)} {plural_steps(len(lesson_pages))}</small></a>'
                f'<div class="index-steps">{step_links}</div></li>'
            )
        section_blocks.append(
            f'<section class="index-section"><header><span>{section["position"]:02d}</span><h2>{html.escape(section["title"].strip())}</h2></header>'
            f'<ol>{"".join(lessons)}</ol></section>'
        )

    return f'''<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Фигма для UX-редакторов — архив курса</title>
  <meta name="description" content="Личный архив курса «Фигма для UX-редакторов»: 8 разделов, 29 уроков и {len(pages)} {plural_steps(len(pages))}.">
  <link rel="icon" type="image/png" sizes="512x512" href="../../assets/favicon.png">
  <link rel="apple-touch-icon" href="../../assets/favicon.png">
  <link rel="stylesheet" href="styles.css">
</head>
<body class="course-index">
  <header class="topbar"><a href="../../index.html">← Все курсы</a><span>Личный учебный архив</span><a href="{COURSE_SOURCE}" target="_blank" rel="noopener">Stepik ↗</a></header>
  <main>
    <section class="course-hero">
      <div><p class="eyebrow">Отдельный курс · Stepik · снимок 2 сентября 2026</p><h1>Фигма для<br>UX‑редакторов</h1><p class="lead">{html.escape(course.get("summary", ""))}</p><div class="stats"><span><b>8</b> разделов</span><span><b>29</b> уроков</span><span><b>{len(pages)}</b> шагов</span></div><a class="primary" href="steps/{page_filename(pages[0])}">Начать с первого шага →</a></div>
      <img src="{html.escape(cover_src, quote=True)}" alt="Обложка курса «Фигма для UX-редакторов»">
    </section>
    <section class="contents"><header><p class="eyebrow">Оглавление</p><h2>Все материалы курса</h2></header>{"".join(section_blocks)}</section>
  </main>
  <footer><span>Авторы курса: Илья Поликарпов и Дена Скульская</span><a href="{COURSE_SOURCE}" target="_blank" rel="noopener">Оригинал курса на Stepik ↗</a></footer>
</body>
</html>
'''


CSS = r'''
:root{--ink:#181719;--paper:#f6f5f1;--white:#fff;--blue:#5246e5;--lime:#dffd62;--muted:#6d6a73;--line:#d8d5cf;--sidebar:350px}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.55}a{color:inherit;text-decoration:none}.topbar{position:sticky;top:0;z-index:20;display:flex;justify-content:space-between;align-items:center;min-height:64px;padding:0 32px;border-bottom:1px solid var(--line);background:rgba(246,245,241,.94);backdrop-filter:blur(12px);font-size:14px;font-weight:700}.course-hero{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(320px,.75fr);gap:7vw;align-items:center;min-height:calc(100vh - 64px);padding:7vw}.course-hero h1{margin:16px 0 28px;font-size:clamp(54px,8vw,128px);font-weight:600;line-height:.88;letter-spacing:-.065em}.course-hero img{width:100%;max-height:650px;object-fit:cover;border-radius:32px;box-shadow:22px 22px 0 var(--lime)}.eyebrow{margin:0;text-transform:uppercase;letter-spacing:.14em;font-size:12px;font-weight:800}.lead{max-width:760px;color:var(--muted);font-size:clamp(18px,2vw,27px)}.stats{display:flex;flex-wrap:wrap;gap:10px;margin:34px 0}.stats span{display:flex;gap:8px;align-items:baseline;padding:12px 16px;border:1px solid var(--line);border-radius:999px}.stats b{font-size:24px}.primary{display:inline-flex;margin-top:8px;padding:16px 24px;border-radius:999px;background:var(--ink);color:#fff;font-weight:800}.contents{padding:8vw 7vw;background:#fff}.contents>header{display:grid;grid-template-columns:1fr 2fr;align-items:end;margin-bottom:64px}.contents>header h2{margin:0;font-size:clamp(42px,6vw,88px);line-height:.95;letter-spacing:-.05em}.index-section{border-top:1px solid var(--ink);padding:30px 0 60px}.index-section>header{display:grid;grid-template-columns:110px 1fr;align-items:start}.index-section>header>span{color:var(--blue);font-weight:800}.index-section h2{margin:0;font-size:clamp(28px,4vw,52px);letter-spacing:-.035em}.index-section ol{margin:32px 0 0 110px;padding:0;list-style:none}.index-section li{display:grid;grid-template-columns:1fr auto;gap:20px;align-items:center;padding:18px 0;border-top:1px solid var(--line)}.index-lesson{display:grid;grid-template-columns:64px 1fr auto;gap:18px;align-items:center}.index-lesson>span{color:var(--muted);font-size:13px}.index-lesson strong{font-size:19px}.index-lesson small{color:var(--muted)}.index-steps{display:flex;gap:5px}.index-steps a,.step-dots a{display:grid;place-items:center;width:29px;height:29px;border:1px solid var(--line);border-radius:50%;font-size:12px}.index-steps a:hover,.step-dots a:hover,.step-dots a.active{background:var(--ink);color:#fff}.course-shell{display:grid;grid-template-columns:var(--sidebar) minmax(0,1fr);min-height:calc(100vh - 64px)}.course-nav{position:sticky;top:64px;height:calc(100vh - 64px);overflow:auto;border-right:1px solid var(--line);background:#fff}.course-home{display:block;padding:22px 24px;border-bottom:1px solid var(--line);font-weight:800}.course-nav details{border-bottom:1px solid var(--line)}.course-nav summary{display:grid;grid-template-columns:44px 1fr;gap:10px;padding:17px 20px;cursor:pointer;font-size:13px;font-weight:800;list-style:none}.course-nav summary::-webkit-details-marker{display:none}.course-nav summary span{color:var(--blue)}.course-nav ol{margin:0;padding:0 14px 16px;list-style:none}.lesson-row{padding:10px;border-radius:12px}.lesson-row:has(.active){background:#f0efe9}.lesson-link{display:grid;grid-template-columns:40px 1fr;gap:8px;font-size:13px}.lesson-link span{color:var(--muted)}.step-dots{display:flex;flex-wrap:wrap;gap:4px;margin:8px 0 0 40px}.step-dots a{width:24px;height:24px}.step-main{width:min(960px,calc(100% - 64px));margin:0 auto;padding:70px 0 100px}.breadcrumb{display:flex;gap:12px;color:var(--blue);font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.12em}.lesson-label{margin:42px 0 10px;color:var(--muted);font-size:13px}.step-main h1{margin:0 0 22px;font-size:clamp(38px,6vw,78px);line-height:1;letter-spacing:-.05em}.source-actions{display:flex;margin-bottom:42px}.source-actions a{border-bottom:1px solid}.archive-note,.empty-note{padding:15px 18px;border-radius:14px;background:#fff1b8;font-size:14px}.video-frame{margin:28px 0 42px;overflow:hidden;border-radius:24px;background:#000}.video-frame video{display:block;width:100%;max-height:70vh}.rich-content{font-size:18px}.rich-content>*:first-child{margin-top:0}.rich-content h1,.rich-content h2,.rich-content h3{margin:2.2em 0 .7em;line-height:1.15;letter-spacing:-.025em}.rich-content h1{font-size:2.4em}.rich-content h2{font-size:1.8em}.rich-content h3{font-size:1.35em}.rich-content p,.rich-content li{max-width:780px}.rich-content img{display:block;max-width:100%;height:auto;margin:28px auto;border-radius:14px}.rich-content table{width:100%;border-collapse:collapse}.rich-content td,.rich-content th{padding:10px;border:1px solid var(--line);vertical-align:top}.rich-content pre,.rich-content code{white-space:pre-wrap;word-break:break-word}.rich-content blockquote{margin:28px 0;padding:18px 24px;border-left:4px solid var(--blue);background:#fff}.rich-content a{color:var(--blue);text-decoration:underline}.rich-content input,.rich-content textarea,.rich-content select{max-width:100%;padding:10px;border:1px solid var(--line);border-radius:8px}.rich-content button{padding:10px 14px;border:0;border-radius:8px;background:var(--ink);color:#fff}.page-nav{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:70px;padding-top:24px;border-top:1px solid var(--line)}.page-nav a:last-child{text-align:right}footer{display:flex;justify-content:space-between;padding:32px 7vw;background:var(--ink);color:#fff;font-size:14px}footer a{color:var(--lime)}@media(max-width:900px){.course-hero{grid-template-columns:1fr;padding-top:12vw}.course-hero img{max-height:420px}.course-shell{grid-template-columns:1fr}.course-nav{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line)}.course-nav details:not([open]){display:none}.course-nav .course-home{display:none}.step-main{width:min(100% - 40px,760px);padding-top:46px}.contents>header{grid-template-columns:1fr;gap:16px}.index-section>header{grid-template-columns:60px 1fr}.index-section ol{margin-left:0}.index-steps{display:none}}@media(max-width:620px){.topbar{padding:0 16px}.topbar span{display:none}.course-hero,.contents{padding-left:20px;padding-right:20px}.course-hero{padding-bottom:70px}.course-hero img{box-shadow:10px 10px 0 var(--lime)}.stats{gap:6px}.index-section>header{grid-template-columns:42px 1fr}.index-section li{grid-template-columns:1fr}.index-lesson{grid-template-columns:46px 1fr}.index-lesson small{grid-column:2}.step-main h1{font-size:42px}.rich-content{font-size:16px}.page-nav{font-size:14px}footer{padding-left:20px;padding-right:20px}}
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("/tmp/codex-stepik-286605-manifest.json"))
    parser.add_argument("--pages", type=Path, default=Path("/tmp/codex-stepik-286605-pages.json"))
    parser.add_argument("--output", type=Path, default=Path("courses") / COURSE_SLUG)
    args = parser.parse_args()

    manifest = read_json(args.manifest)
    pages = read_json(args.pages)
    if not pages or any(page.get("error") for page in pages):
        raise SystemExit("The browser export is incomplete.")

    output = args.output.resolve()
    if output.exists():
        shutil.rmtree(output)
    (output / "steps").mkdir(parents=True)
    assets = output / "assets"

    originals = collect_asset_urls(manifest, pages)
    asset_map, failures = download_assets(originals, assets)
    cover = manifest["course"].get("cover", "")
    if cover in asset_map:
        shutil.copyfile(assets / asset_map[cover], assets / "cover.png")
        asset_map[cover] = "cover.png"

    for index, page in enumerate(pages):
        (output / "steps" / page_filename(page)).write_text(
            render_step(page, pages, index, asset_map), encoding="utf-8"
        )

    (output / "index.html").write_text(render_course_index(manifest, pages, asset_map), encoding="utf-8")
    (output / "styles.css").write_text(CSS.strip() + "\n", encoding="utf-8")
    (output / "asset-errors.txt").write_text("\n".join(failures) + ("\n" if failures else ""), encoding="utf-8")
    print(f"Built {len(pages)} pages, downloaded {len(set(asset_map.values()))} assets, failures: {len(failures)}")


if __name__ == "__main__":
    main()
