#!/usr/bin/env python3
"""Build the leadership competency guide from the verbatim Playbook pages.

Requires lxml. Pass a directory containing the six downloaded source pages.
"""

from __future__ import annotations

import argparse
import html as html_std
import re
from pathlib import Path
from urllib.parse import urljoin

from lxml import etree, html


BASE_URL = "https://leadership-playbook.avito.com/"
PAGES = [
    ("clarity", "development_of_eadership_competencies_reates_clarity", "Создаёт ясность"),
    ("forward", "development_of_eadership_competencies_moving_forward", "Продвигает вперёд"),
    ("change", "development_of_eadership_competencies_promotes_change", "Способствует изменениям"),
    ("bar", "development_of_eadership_competencies_promotes_raises_bar", "Повышает планку"),
    ("team", "development_of_eadership_competencies_promotes_strengthens_team", "Усиливает команду"),
    ("owner", "development_of_eadership_competencies_promotes_acts_as_co-owner", "Действует как совладелец Авито"),
]
TRACKS = [
    ("work", "На рабочем месте · 70%"),
    ("people", "Опыт других · 20%"),
    ("learning", "Обучение · 10%"),
]


def number(value: str | None, fallback: int = 0) -> int:
    try:
        return int(float(value or fallback))
    except ValueError:
        return fallback


def has_class(node, name: str) -> bool:
    return name in (node.get("class") or "").split()


def record_atoms(record):
    atoms = []
    for element in record.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " t396__elem ")]'):
        direct = element.xpath('./*[contains(concat(" ", normalize-space(@class), " "), " tn-atom ")]')
        if not direct:
            continue
        atom = direct[0]
        text = " ".join(atom.text_content().split())
        if not text:
            continue
        atoms.append((number(element.get("data-field-top-value")), number(element.get("data-field-left-value")), atom, text))
    return sorted(atoms, key=lambda item: (item[0], item[1]))


def inner_html(node) -> str:
    parts = [node.text or ""]
    for child in node:
        parts.append(etree.tostring(child, encoding="unicode", method="html", with_tail=True))
    return "".join(parts)


def clean_fragment(atom) -> str:
    wrapper = html.fragment_fromstring(f"<div>{inner_html(atom)}</div>")
    for bad in wrapper.xpath('.//script|.//style|.//img|.//svg'):
        bad.drop_tree()
    for node in wrapper.iter():
        if node.tag == "a" and node.get("href"):
            node.set("href", urljoin(BASE_URL, node.get("href")))
            if node.get("href", "").startswith(("http://", "https://")):
                node.set("target", "_blank")
                node.set("rel", "noopener")
        for attribute in list(node.attrib):
            if node.tag == "a" and attribute in {"href", "target", "rel"}:
                continue
            del node.attrib[attribute]
        if node.tag == "span":
            node.drop_tag()
    result = inner_html(wrapper).strip()
    return result


def text_atoms(record):
    return record_atoms(record)


def find_indicator_record(tab_record):
    current = tab_record.getprevious()
    while current is not None:
        texts = [item[3] for item in text_atoms(current)]
        if any(text.casefold() == "индикатор" for text in texts):
            return current
        current = current.getprevious()
    raise RuntimeError(f"Indicator heading not found before {tab_record.get('id')}")


def indicator_title(record) -> str:
    candidates = [item[3] for item in text_atoms(record) if item[3].casefold() != "индикатор"]
    if not candidates:
        raise RuntimeError(f"Indicator title is empty in {record.get('id')}")
    return max(candidates, key=len)


def practice_title(record) -> str:
    candidates = [item[3] for item in text_atoms(record)]
    if not candidates:
        raise RuntimeError(f"Practice title is empty in {record.get('id')}")
    return max(candidates, key=len)


def practice_body(record) -> str:
    blocks = []
    for _top, left, atom, text in text_atoms(record):
        fragment = clean_fragment(atom)
        if not fragment:
            continue
        if left < 120 and len(text) < 90:
            blocks.append(f'<h5>{html_std.escape(text)}</h5>')
        else:
            blocks.append(f'<div class="source-copy">{fragment}</div>')
    return "".join(blocks)


def record_by_number(root, record_number: str):
    found = root.xpath(f'//*[@id="rec{record_number}"]')
    if not found:
        raise RuntimeError(f"Source record rec{record_number} not found")
    return found[0]


def extract_page(source: Path, page_id: str, fallback_title: str, source_url: str):
    root = html.parse(str(source))
    indicators = []
    tab_records = root.xpath('//div[@data-record-type="1281"]')
    if len(tab_records) != 3:
        raise RuntimeError(f"Expected 3 indicators in {source.name}, got {len(tab_records)}")
    for indicator_index, tab_record in enumerate(tab_records, start=1):
        title = indicator_title(find_indicator_record(tab_record))
        tracks = []
        groups = tab_record.xpath('.//*[@data-tab-rec-ids]')
        if len(groups) != 3:
            raise RuntimeError(f"Expected 3 tracks for {title}, got {len(groups)}")
        for (track_id, track_label), group in zip(TRACKS, groups):
            ids = [
                item.strip()
                for item in group.get("data-tab-rec-ids").split(",")
                if item.strip() and root.xpath(f'//*[@id="rec{item.strip()}"]')
            ]
            if len(ids) % 2:
                raise RuntimeError(f"Odd source record count for {title}: {ids}")
            practices = []
            for position in range(0, len(ids), 2):
                heading_record = record_by_number(root, ids[position])
                body_record = record_by_number(root, ids[position + 1])
                practices.append({
                    "title": practice_title(heading_record),
                    "body": practice_body(body_record),
                })
            tracks.append({"id": track_id, "label": track_label, "practices": practices})
        indicators.append({"number": indicator_index, "title": title, "tracks": tracks})
    title_nodes = root.xpath('//title/text()')
    title = (title_nodes[0].strip() if title_nodes else fallback_title).replace(" | Leadership Playbook", "")
    return {"id": page_id, "title": title, "source_url": source_url, "indicators": indicators}


def render_practice(practice: dict, number_value: int) -> str:
    return (
        '<article class="practice">'
        f'<header><span>{number_value:02d}</span><h4>{html_std.escape(practice["title"])}</h4></header>'
        f'<div class="practice-body">{practice["body"]}</div>'
        '</article>'
    )


def practice_label(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        word = "практика"
    elif count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        word = "практики"
    else:
        word = "практик"
    return f"{count} {word}"


def render_indicator(page_id: str, indicator: dict) -> str:
    panels = []
    tab_buttons = []
    for track_index, track in enumerate(indicator["tracks"]):
        selected = "true" if track_index == 0 else "false"
        hidden = "" if track_index == 0 else " hidden"
        tab_buttons.append(
            f'<button type="button" role="tab" aria-selected="{selected}" data-track="{track["id"]}">'
            f'{html_std.escape(track["label"])}<small>{practice_label(len(track["practices"]))}</small></button>'
        )
        practices = "".join(render_practice(practice, practice_index) for practice_index, practice in enumerate(track["practices"], start=1))
        panels.append(
            f'<section class="track-panel" role="tabpanel" data-track="{track["id"]}"{hidden}>'
            f'<p class="track-count">Все практики раздела · {len(track["practices"])}</p>{practices}</section>'
        )
    return (
        f'<article class="indicator-card" id="{page_id}-indicator-{indicator["number"]}" data-indicator="{indicator["number"]}">'
        '<header class="indicator-head">'
        f'<span>{indicator["number"]:02d}</span><h3>{html_std.escape(indicator["title"])}</h3>'
        '</header>'
        f'<div class="indicator-tabs" role="tablist">{"".join(tab_buttons)}</div>'
        f'{"".join(panels)}'
        '</article>'
    )


def render_page(page: dict, page_index: int) -> str:
    nav = "".join(
        f'<button type="button" data-indicator-target="{page["id"]}-indicator-{indicator["number"]}"'
        f' aria-current="{"true" if indicator["number"] == 1 else "false"}">'
        f'<small>Индикатор {indicator["number"]:02d}</small>{html_std.escape(indicator["title"])}</button>'
        for indicator in page["indicators"]
    )
    indicators = "".join(render_indicator(page["id"], indicator) for indicator in page["indicators"])
    return (
        f'<section class="competency-view" id="{page["id"]}" data-name="{html_std.escape(page["title"])}"'
        f'{"" if page_index == 1 else " hidden"}>'
        '<header class="view-head">'
        f'<span>{page_index:02d} / 06</span><h2>{html_std.escape(page["title"])}</h2>'
        f'<a href="{page["source_url"]}" target="_blank" rel="noopener">Оригинал ↗</a>'
        '</header>'
        f'<nav class="indicator-nav" aria-label="Индикаторы компетенции">{nav}</nav>'
        f'<div class="indicator-grid">{indicators}</div>'
        '</section>'
    )


def build_document(pages: list[dict]) -> str:
    competency_tabs = "".join(
        f'<button type="button" role="tab" aria-selected="{"true" if index == 1 else "false"}" data-target="{page["id"]}">'
        f'<span>{index:02d}</span>{html_std.escape(page["title"])}</button>'
        for index, page in enumerate(pages, start=1)
    )
    views = "".join(render_page(page, index) for index, page in enumerate(pages, start=1))
    practice_count = sum(
        len(track["practices"])
        for page in pages
        for indicator in page["indicators"]
        for track in indicator["tracks"]
    )
    return f'''<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="Полный гид по развитию шести лидерских компетенций Авито: 18 индикаторов и все практики 70/20/10.">
  <title>Лидерские компетенции · First Time Management</title>
  <link rel="icon" type="image/png" sizes="512x512" href="../assets/favicon.png">
  <link rel="apple-touch-icon" href="../assets/favicon.png">
  <style>
    :root{{--ink:#222228;--paper:#fbfbf8;--white:#fff;--acid:#eefcaf;--mint:#dcf5e8;--line:rgba(34,34,40,.18);--muted:#6d6e72;--gutter:max(20px,calc((100vw - 1320px)/2))}}
    *{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;line-height:1.48;-webkit-font-smoothing:antialiased}}a{{color:inherit}}button{{color:inherit}}[hidden]{{display:none!important}}
    .topbar{{position:sticky;top:0;z-index:20;display:flex;justify-content:space-between;align-items:center;min-height:70px;padding:0 var(--gutter);border-bottom:1px solid var(--line);background:rgba(251,251,248,.94);backdrop-filter:blur(14px);font-size:12px;letter-spacing:.07em;text-transform:uppercase}}.topbar a{{text-decoration:none}}
    .hero{{display:grid;grid-template-columns:1.35fr .65fr;gap:60px;width:min(1320px,calc(100% - 40px));margin:0 auto;padding:76px 0 56px}}.eyebrow{{margin:0 0 20px;font-size:11px;font-weight:750;letter-spacing:.14em;text-transform:uppercase}}.hero h1{{max-width:980px;margin:0;font-size:clamp(58px,8.5vw,120px);font-weight:520;line-height:.88;letter-spacing:-.07em}}.lead{{max-width:820px;margin:32px 0 0;font-size:clamp(19px,2vw,28px);line-height:1.25}}.hero-note{{align-self:end;padding:26px;border:1px solid var(--line);border-radius:22px;background:var(--mint)}}.hero-note strong{{display:block;font-size:42px;letter-spacing:-.05em}}.hero-note p{{margin:8px 0 0;color:#4f5652}}
    .controls{{position:sticky;top:70px;z-index:15;padding:12px var(--gutter);border-block:1px solid var(--line);background:rgba(251,251,248,.94);backdrop-filter:blur(14px)}}.competency-tabs{{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;max-width:1320px;margin:auto}}.competency-tabs button{{min-height:64px;padding:10px 12px;border:1px solid var(--line);border-radius:14px;background:transparent;font:inherit;font-size:12px;font-weight:700;line-height:1.2;text-align:left;cursor:pointer}}.competency-tabs button span{{display:block;margin-bottom:5px;color:var(--muted);font-size:10px}}.competency-tabs button[aria-selected="true"]{{border-color:var(--ink);background:var(--ink);color:#fff}}
    .views{{width:min(1320px,calc(100% - 40px));margin:0 auto;padding:68px 0 120px}}.competency-view{{scroll-margin-top:166px}}.view-head{{display:grid;grid-template-columns:100px 1fr auto;gap:24px;align-items:end;margin-bottom:28px}}.view-head>span{{color:var(--muted)}}.view-head h2{{margin:0;font-size:clamp(48px,7vw,102px);font-weight:520;line-height:.9;letter-spacing:-.06em}}.view-head a{{font-size:12px;font-weight:750;letter-spacing:.1em;text-transform:uppercase}}
    .indicator-nav{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:22px}}.indicator-nav button{{min-height:94px;padding:15px 18px;border:1px solid var(--ink);border-radius:18px;background:transparent;font:inherit;font-size:14px;font-weight:700;line-height:1.25;text-align:left;cursor:pointer}}.indicator-nav button small{{display:block;margin-bottom:7px;color:var(--muted);font-size:10px;letter-spacing:.11em;text-transform:uppercase}}.indicator-nav button[aria-current="true"]{{background:var(--acid)}}
    .indicator-card{{padding:30px;border:1px solid var(--ink);border-radius:28px;background:var(--white);scroll-margin-top:170px}}.indicator-head{{display:grid;grid-template-columns:76px 1fr;gap:24px;align-items:start}}.indicator-head>span{{font-size:36px;letter-spacing:-.05em}}.indicator-head h3{{max-width:1060px;margin:0;font-size:clamp(30px,4vw,58px);font-weight:540;line-height:1.04;letter-spacing:-.045em}}
    .indicator-tabs{{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:30px 0 18px;padding:6px;border:1px solid var(--ink);border-radius:16px;background:var(--paper)}}.indicator-tabs button{{display:flex;justify-content:space-between;align-items:center;gap:12px;min-height:58px;padding:10px 15px;border:0;border-radius:11px;background:transparent;font:inherit;font-size:14px;font-weight:750;cursor:pointer}}.indicator-tabs button small{{font-size:10px;font-weight:650;opacity:.66}}.indicator-tabs button[aria-selected="true"]{{background:var(--ink);color:#fff}}
    .track-count{{margin:0 0 12px;color:var(--muted);font-size:11px;font-weight:750;letter-spacing:.12em;text-transform:uppercase}}.track-panel{{display:grid;gap:12px}}.practice{{border:1px solid var(--line);border-radius:20px;overflow:hidden;background:#faf9f6}}.practice>header{{display:grid;grid-template-columns:52px 1fr;gap:14px;align-items:center;padding:17px 20px;background:var(--mint)}}.practice>header span{{font-size:13px;font-weight:750}}.practice h4{{margin:0;font-size:19px;font-weight:720}}.practice-body{{display:grid;grid-template-columns:160px minmax(0,1fr);gap:14px 22px;padding:24px}}.practice-body h5{{margin:0;color:#39745e;font-size:11px;letter-spacing:.13em;text-transform:uppercase}}.source-copy{{grid-column:2;margin:0;font-size:16px;line-height:1.52}}.source-copy:first-child{{grid-column:1/-1}}.source-copy ul,.source-copy ol{{margin:10px 0;padding-left:22px}}.source-copy li+li{{margin-top:5px}}.source-copy a{{text-decoration-thickness:1px;text-underline-offset:3px}}.source-copy br+br{{display:block;margin-top:9px;content:""}}
    .footer-note{{display:flex;justify-content:space-between;gap:28px;padding:30px var(--gutter);border-top:1px solid var(--line);color:var(--muted);font-size:12px}}
    :focus-visible{{outline:3px solid #8bb700;outline-offset:3px}}
    @media(max-width:1000px){{.hero{{grid-template-columns:1fr}}.hero-note{{max-width:620px}}.competency-tabs{{display:flex;overflow:auto}}.competency-tabs button{{flex:0 0 180px}}.view-head{{grid-template-columns:70px 1fr}}.view-head a{{grid-column:2}}.indicator-nav{{grid-template-columns:1fr}}}}
    @media(max-width:680px){{.topbar{{padding-inline:16px}}.hero,.views{{width:calc(100% - 24px)}}.hero{{padding-top:48px}}.controls{{top:70px;padding-inline:12px}}.views{{padding-top:48px}}.view-head{{grid-template-columns:1fr;gap:8px}}.view-head a{{grid-column:1}}.indicator-nav{{display:flex;overflow:auto}}.indicator-nav button{{flex:0 0 260px}}.indicator-card{{padding:18px;scroll-margin-top:160px}}.indicator-head{{grid-template-columns:1fr;gap:8px}}.indicator-tabs{{display:flex;overflow:auto}}.indicator-tabs button{{flex:0 0 220px}}.practice-body{{grid-template-columns:1fr;padding:20px}}.practice-body h5,.source-copy{{grid-column:1}}.footer-note{{flex-direction:column}}}}
  </style>
</head>
<body>
  <header class="topbar"><a href="../">← First Time Management</a><span>Полная версия · источник Leadership Playbook</span></header>
  <main>
    <section class="hero"><div><p class="eyebrow">Лидерские компетенции Авито</p><h1>Все практики развития</h1><p class="lead">Шесть компетенций, 18 индикаторов и полный набор практик из Leadership Playbook. Выбери компетенцию, затем индикатор и способ развития.</p></div><aside class="hero-note"><strong>{practice_count}</strong><p>практик перенесено в полном объёме: рабочие задачи, опыт других и формализованное обучение.</p></aside></section>
    <div class="controls"><div class="competency-tabs" role="tablist" aria-label="Выбор компетенции">{competency_tabs}</div></div>
    <div class="views">{views}</div>
  </main>
  <footer class="footer-note"><span>Текст практик перенесён без сокращений из Leadership Playbook.</span><a href="{BASE_URL}guide_of_leadership_competence_development" target="_blank" rel="noopener">Открыть оригинал ↗</a></footer>
  <script>
    const competencyButtons=[...document.querySelectorAll('.competency-tabs button')];
    const views=[...document.querySelectorAll('.competency-view')];
    function selectIndicator(view,targetId,scroll=false){{
      view.querySelectorAll('[data-indicator-target]').forEach(button=>button.setAttribute('aria-current',String(button.dataset.indicatorTarget===targetId)));
      if(scroll) document.getElementById(targetId)?.scrollIntoView({{behavior:'smooth',block:'start'}});
    }}
    function selectCompetency(id,pushHash=true){{
      competencyButtons.forEach(button=>button.setAttribute('aria-selected',String(button.dataset.target===id)));
      views.forEach(view=>view.hidden=view.id!==id);
      const view=document.getElementById(id);
      const first=view?.querySelector('.indicator-card');
      if(view&&first) selectIndicator(view,first.id,false);
      if(pushHash){{history.replaceState(null,'','#'+id);view?.scrollIntoView({{behavior:'smooth',block:'start'}})}}
    }}
    competencyButtons.forEach(button=>button.addEventListener('click',()=>selectCompetency(button.dataset.target)));
    document.querySelectorAll('.indicator-nav').forEach(nav=>nav.addEventListener('click',event=>{{const button=event.target.closest('[data-indicator-target]');if(button) selectIndicator(nav.closest('.competency-view'),button.dataset.indicatorTarget,true)}}));
    document.querySelectorAll('.indicator-tabs').forEach(tabs=>tabs.addEventListener('click',event=>{{
      const button=event.target.closest('[data-track]'); if(!button) return;
      const card=tabs.closest('.indicator-card');
      tabs.querySelectorAll('button').forEach(item=>item.setAttribute('aria-selected',String(item===button)));
      card.querySelectorAll('.track-panel').forEach(panel=>panel.hidden=panel.dataset.track!==button.dataset.track);
    }}));
    const initial=location.hash.slice(1); if(initial&&document.getElementById(initial)?.classList.contains('competency-view')) selectCompetency(initial,false);
  </script>
</body>
</html>'''


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pages = []
    for page_id, slug, fallback_title in PAGES:
        source = args.source_dir / f"{slug}.html"
        if not source.exists():
            raise SystemExit(f"Missing source file: {source}")
        pages.append(extract_page(source, page_id, fallback_title, urljoin(BASE_URL, slug)))
    output = build_document(pages)
    args.output.write_text(output, encoding="utf-8")
    print(f"Wrote {args.output}: {len(output):,} chars")


if __name__ == "__main__":
    main()
