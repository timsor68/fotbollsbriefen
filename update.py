#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import email.utils
import html
import json
import re
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
OUT = ROOT / "news.json"
UA = "Mozilla/5.0 Fotbollsbriefen/2.2"
NOW = dt.datetime.now(dt.timezone.utc)
CUTOFF = NOW - dt.timedelta(days=CFG.get("max_age_days", 7))

RESULT_WORDS = re.compile(
    r"\b(score|result|highlights|standings|table|match report|beat|defeat|draw|"
    r"full-time|final score|vs|preview|reaction)\b",
    re.I,
)
JUNK_WORDS = re.compile(
    r"\b(shop|store|shirt|jersey|kit|merch|merchandise|tickets?|hospitality|"
    r"gallery|training gallery|inside training|watch now|video|quiz|wallpaper|"
    r"download|membership|museum|tour|matchday programme|programme|podcast|"
    r"fixture|fixtures|kick-off|kickoff|line-up|lineup|starting xi|team news|"
    r"live blog|live updates|world squad|academy|under-18|u18|under-21|u21|kids?)\b",
    re.I,
)
NEWS_WORDS = re.compile(
    r"\b(sign|signs|signed|transfer|joins|joined|appoint|appointed|sack|sacked|"
    r"leaves|left|contract|renew|extension|injury|injured|operation|surgery|"
    r"director|sporting director|manager|coach|chief executive|ceo|ban|suspension|"
    r"investigation|fine|takeover|ownership|bid|agreement|deal|medical|loan|"
    r"release clause|retire|retirement|statement|confirmed|official)\b",
    re.I,
)

OFFICIAL = {x["name"] for x in CFG["feeds"] if x.get("priority", 0) >= 100}


def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(
        req, timeout=timeout, context=ssl.create_default_context()
    ) as response:
        return response.read()


def node_text(node: ET.Element, names: list[str]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def clean(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return re.sub(r"\s+", " ", value).strip()


def parse_date(value: str) -> dt.datetime | None:
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        try:
            return dt.datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).astimezone(dt.timezone.utc)
        except Exception:
            return None


def google_url(query: str) -> str:
    query = f'{query} when:{CFG.get("max_age_days", 7)}d'
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"}
    )


def categories(title: str) -> list[str]:
    lowered = title.lower()
    output: list[str] = []
    pairs = [
        ("Premier League", r"manchester|liverpool|arsenal|chelsea|tottenham|newcastle|aston villa|premier league"),
        ("La Liga", r"barcelona|real madrid|atl[eé]tico|la liga"),
        ("Serie A", r"juventus|milan|inter|napoli|roma|lazio|atalanta|serie a"),
        ("Bundesliga", r"bayern|dortmund|leverkusen|bundesliga"),
        ("Ligue 1", r"psg|paris saint|marseille|monaco|ligue 1"),
        ("Transfer", r"transfer|sign|deal|bid|joins|move|contract|renew"),
        ("Tränare", r"manager|coach|head coach|trainer"),
    ]
    for category, pattern in pairs:
        if re.search(pattern, lowered):
            output.append(category)
    return output or ["Fotboll"]


def source_status(source: str) -> str:
    if source in OFFICIAL:
        return "Officiellt"
    if source in {"Reuters", "BBC Sport", "Simon Stone / BBC Sport"}:
        return "Bekräftat"
    if source in {
        "David Ornstein / The Athletic",
        "Fabrizio Romano",
        "Gianluca Di Marzio",
    }:
        return "Mycket trovärdigt"
    return "Rapporterat"


def normalized(value: str) -> str:
    value = re.sub(r"[^a-z0-9 ]", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


def short_summary(description: str, title: str) -> str:
    description = clean(description)
    if not description:
        return f"{title}. Läs mer hos källan."
    sentences = re.split(r"(?<=[.!?])\s+", description)
    return " ".join(sentences[:4]).strip()[:850]


def news_score(title: str, description: str, source: dict) -> int:
    combined = f"{title} {description}"
    score = int(source.get("priority", 50))

    if NEWS_WORDS.search(combined):
        score += 35
    if source.get("priority", 0) >= 100 and NEWS_WORDS.search(combined):
        score += 20
    if source["name"] in {
        "Reuters",
        "BBC Sport",
        "Simon Stone / BBC Sport",
        "David Ornstein / The Athletic",
        "Fabrizio Romano",
        "Gianluca Di Marzio",
    }:
        score += 20
    if JUNK_WORDS.search(combined):
        score -= 120
    if len(clean(description)) < 45:
        score -= 20

    return score


items: list[dict] = []

for source in CFG["feeds"]:
    url = source["url"] if source["type"] == "rss" else google_url(source["query"])
    try:
        xml_root = ET.fromstring(fetch(url))
    except Exception as exc:
        print("WARN", source["name"], exc)
        continue

    for node in xml_root.findall(".//item"):
        title = clean(node_text(node, ["title"]))
        link = clean(node_text(node, ["link"]))
        published = parse_date(node_text(node, ["pubDate", "published", "updated"]))
        description = node_text(node, ["description", "summary"])

        if (
            not title
            or not link
            or not published
            or published < CUTOFF
            or RESULT_WORDS.search(title)
            or JUNK_WORDS.search(f"{title} {description}")
        ):
            continue

        if " - " in title and source["type"] == "google":
            title = title.rsplit(" - ", 1)[0].strip()

        score = news_score(title, description, source)
        if score < 70:
            continue

        category_list = categories(title)
        items.append(
            {
                "published_at": published.isoformat(),
                "updated_at": published.isoformat(),
                "title": title,
                "summary": short_summary(description, title),
                "category": category_list,
                "status": source_status(source["name"]),
                "entity": "",
                "entity_type": "coaches" if "Tränare" in category_list else "players",
                "image": "",
                "source_priority": score,
                "sources": [{"name": source["name"], "url": link}],
            }
        )

items.sort(
    key=lambda item: (item["source_priority"], item["published_at"]),
    reverse=True,
)

groups: list[list[dict]] = []

for item in items:
    match = None
    for group in groups:
        ratio = SequenceMatcher(
            None, normalized(item["title"]), normalized(group[0]["title"])
        ).ratio()
        if ratio >= 0.72:
            match = group
            break

    if match is not None:
        match.append(item)
    else:
        groups.append([item])

merged: list[dict] = []

for group in groups:
    group.sort(key=lambda item: item["source_priority"], reverse=True)
    lead = dict(group[0])
    source_names: set[str] = set()
    merged_sources: list[dict] = []

    for item in group:
        for source in item["sources"]:
            if source["name"] not in source_names:
                source_names.add(source["name"])
                merged_sources.append(source)

    lead["sources"] = merged_sources
    if any(item["status"] == "Officiellt" for item in group):
        lead["status"] = "Officiellt"
    elif len(merged_sources) > 1:
        lead["status"] = "Bekräftat av flera källor"

    lead["rank_score"] = max(
        item["source_priority"] for item in group
    ) + min(30, (len(merged_sources) - 1) * 10)
    lead["id"] = re.sub(
        r"[^a-z0-9]+", "-", normalized(lead["title"])
    ).strip("-")[:90]
    lead.pop("source_priority", None)
    merged.append(lead)

merged.sort(
    key=lambda item: (item["rank_score"], item["published_at"]),
    reverse=True,
)

final_items: list[dict] = []
for item in merged[: CFG.get("max_items", 120)]:
    item = dict(item)
    item.pop("rank_score", None)
    final_items.append(item)

if not final_items:
    print("No acceptable news found; keeping existing news.json unchanged")
    raise SystemExit(0)

payload = {"updated_at": NOW.isoformat(), "items": final_items}
OUT.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
print("Wrote", len(final_items), "items")
