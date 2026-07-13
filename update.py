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
import concurrent.futures
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
OUT = ROOT / "news.json"

UA = "Mozilla/5.0 Fotbollsbriefen/3.0"
NOW = dt.datetime.now(dt.timezone.utc)
CUTOFF = NOW - dt.timedelta(days=CFG.get("max_age_days", 7))
MAX_ITEMS = min(int(CFG.get("max_items", 120)), 70)

RESULT_WORDS = re.compile(
    r"\b(score|result|highlights|standings|table|match report|beat|defeat|draw|"
    r"full-time|final score|preview|reaction|quarter-finals?|semi-finals?|final)\b",
    re.I,
)

JUNK_WORDS = re.compile(
    r"\b(shop|store|shirt|jersey|kit|merch|merchandise|tickets?|hospitality|"
    r"gallery|training gallery|inside training|watch now|video|quiz|wallpaper|"
    r"download|membership|museum|tour|matchday programme|programme|podcast|"
    r"fixture|fixtures|kick-off|kickoff|line-up|lineup|starting xi|team news|"
    r"live blog|live updates|world squad|academy|under-18|u18|under-21|u21|kids?|"
    r"pre-season preparations|back to work|who is .* idol|raising the levels)\b",
    re.I,
)

NEWS_WORDS = re.compile(
    r"\b(sign|signs|signed|transfer|joins|joined|appoint|appointed|sack|sacked|"
    r"leaves|left|contract|renew|extension|injury|injured|operation|surgery|"
    r"director|sporting director|manager|coach|chief executive|ceo|ban|suspension|"
    r"investigation|fine|takeover|ownership|bid|agreement|deal|medical|loan|"
    r"release clause|retire|retirement|statement|confirmed|official|agrees|"
    r"agreement|close in|talks|negotiations|offer|rejects?|accepts?)\b",
    re.I,
)

LOW_VALUE_TITLE = re.compile(
    r"\b(transfer news today|latest transfer news|round-up|daily round-up|"
    r"what we learned|five things|everything you need to know|explained|"
    r"all you need to know|when are|how to watch|official website|club website|"
    r"on pictures|in pictures|photo gallery|press conference|latest\.|breaking news today)\b",
    re.I,
)

BLOCKED_PUBLISHERS = {
    "Business Upturn",
    "FootballTransfers",
    "Sports Mole",
    "CaughtOffside",
    "TEAMtalk",
    "Transfer Tavern",
    "Football365",
    "OneFootball",
    "Yahoo Sports",
}

PRIMARY_SOURCES = {
    "Reuters": 100,
    "BBC Sport": 98,
    "Simon Stone / BBC Sport": 98,
    "David Ornstein / The Athletic": 98,
    "The Athletic": 96,
    "Fabrizio Romano": 95,
    "Gianluca Di Marzio": 94,
    "Sky Sports": 90,
    "The Guardian": 87,
    "ESPN": 85,
    "Kicker": 88,
    "L'Équipe": 88,
    "RMC Sport": 87,
    "Football Italia": 84,
    "Marca": 80,
    "AS": 80,
    "Mundo Deportivo": 79,
}

OFFICIAL_NAMES = {
    x["name"] for x in CFG["feeds"] if x.get("priority", 0) >= 100
}

STOPWORDS = {
    "the", "and", "for", "from", "with", "into", "over", "after", "before",
    "amid", "about", "near", "close", "sign", "signs", "signed", "transfer",
    "news", "official", "football", "club", "deal", "move", "talks", "report",
    "reports", "latest", "today", "why", "what", "how", "when", "this", "that",
}


def fetch(url: str, timeout: int = 22) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(
        request,
        timeout=timeout,
        context=ssl.create_default_context(),
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


def split_google_title(raw_title: str) -> tuple[str, str | None]:
    if " - " not in raw_title:
        return raw_title.strip(), None
    title, publisher = raw_title.rsplit(" - ", 1)
    return title.strip(), publisher.strip()


def normalize_publisher(value: str) -> str:
    aliases = {
        "BBC": "BBC Sport",
        "BBC.com": "BBC Sport",
        "The Athletic - The New York Times": "The Athletic",
        "Sky Sports Football": "Sky Sports",
        "Reuters.com": "Reuters",
        "GianlucaDiMarzio.com": "Gianluca Di Marzio",
    }
    return aliases.get(value, value)


def categories(title: str) -> list[str]:
    lowered = title.lower()
    output: list[str] = []
    pairs = [
        ("Premier League", r"manchester|liverpool|arsenal|chelsea|tottenham|newcastle|aston villa|premier league|wolves|brentford|everton|leeds"),
        ("La Liga", r"barcelona|real madrid|atl[eé]tico|la liga|sevilla|villarreal|valencia"),
        ("Serie A", r"juventus|milan|inter|napoli|roma|lazio|atalanta|fiorentina|torino|serie a"),
        ("Bundesliga", r"bayern|dortmund|leverkusen|bundesliga|leipzig|frankfurt"),
        ("Ligue 1", r"psg|paris saint|marseille|monaco|lyon|ligue 1"),
        ("Transfer", r"transfer|sign|deal|bid|joins|move|contract|renew|loan|medical|talks|agreement"),
        ("Tränare", r"manager|coach|head coach|trainer|sack|appointed"),
        ("Klubbledning", r"sporting director|director of football|chief executive|ceo|owner|ownership"),
    ]
    for category, pattern in pairs:
        if re.search(pattern, lowered):
            output.append(category)
    return output or ["Fotboll"]


def source_status(source: str, source_count: int) -> str:
    if source in OFFICIAL_NAMES:
        return "Officiellt"
    if source_count >= 2:
        return "Bekräftat av flera källor"
    if source in {"Reuters", "BBC Sport", "Simon Stone / BBC Sport"}:
        return "Bekräftat"
    if source in {
        "David Ornstein / The Athletic",
        "Fabrizio Romano",
        "Gianluca Di Marzio",
        "The Athletic",
    }:
        return "Mycket trovärdigt"
    return "Rapporterat"


def normalized(value: str) -> str:
    value = re.sub(r"[^a-z0-9åäö ]", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


def significant_words(value: str) -> set[str]:
    words = re.findall(r"[a-zåäö0-9]+", normalized(value))
    return {word for word in words if len(word) >= 4 and word not in STOPWORDS}


def short_summary(description: str, title: str) -> str:
    description = clean(description)
    if not description:
        return f"{title}. Läs mer hos källan."
    description = re.sub(
        r"\b(Business Upturn|FootballTransfers|Sports Mole|CaughtOffside)\b",
        "",
        description,
        flags=re.I,
    )
    sentences = re.split(r"(?<=[.!?])\s+", description)
    selected: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        sentence = sentence.strip()
        key = normalized(sentence)[:120]
        if len(sentence) < 25 or key in seen:
            continue
        seen.add(key)
        selected.append(sentence)
        if len(selected) >= 4:
            break
    result = " ".join(selected).strip()
    return (result or f"{title}. Läs mer hos källan.")[:850]


def detect_named_journalist(title: str, description: str) -> str | None:
    combined = f"{title} {description}"
    if re.search(r"\bDavid Ornstein\b", combined, re.I):
        return "David Ornstein / The Athletic"
    if re.search(r"\bFabrizio Romano\b", combined, re.I):
        return "Fabrizio Romano"
    if re.search(r"\bGianluca Di Marzio\b|\bDi Marzio\b", combined, re.I):
        return "Gianluca Di Marzio"
    if re.search(r"\bSimon Stone\b", combined, re.I):
        return "Simon Stone / BBC Sport"
    return None


def source_score(source: str, configured_priority: int) -> int:
    if source in OFFICIAL_NAMES:
        return 100
    return PRIMARY_SOURCES.get(source, configured_priority)


def news_score(
    title: str,
    description: str,
    source: str,
    configured_priority: int,
) -> int:
    combined = f"{title} {description}"
    score = source_score(source, configured_priority)

    if NEWS_WORDS.search(combined):
        score += 35
    if source in OFFICIAL_NAMES and NEWS_WORDS.search(combined):
        score += 15
    if source in PRIMARY_SOURCES:
        score += 15
    if LOW_VALUE_TITLE.search(title):
        score -= 60
    if JUNK_WORDS.search(combined):
        score -= 150
    if len(clean(description)) < 55:
        score -= 20

    return score


def similar_story(first: dict, second: dict) -> bool:
    ratio = SequenceMatcher(
        None,
        normalized(first["title"]),
        normalized(second["title"]),
    ).ratio()
    if ratio >= 0.68:
        return True

    first_words = significant_words(first["title"])
    second_words = significant_words(second["title"])
    overlap = first_words & second_words

    if len(overlap) >= 3:
        return True

    # Strong entity-style overlap: same two uncommon names plus transfer context.
    if len(overlap) >= 2 and (
        "Transfer" in first["category"] or "Transfer" in second["category"]
    ):
        return True

    return False


def read_existing() -> list[dict]:
    try:
        payload = json.loads(OUT.read_text(encoding="utf-8"))
        result = []
        for item in payload.get("items", []):
            published = parse_date(item.get("published_at", ""))
            if published and published >= CUTOFF:
                item = dict(item)
                item["source_priority"] = 60
                result.append(item)
        return result
    except Exception:
        return []


items: list[dict] = []

for configured_source in CFG["feeds"]:
    url = (
        configured_source["url"]
        if configured_source["type"] == "rss"
        else google_url(configured_source["query"])
    )

    try:
        xml_root = ET.fromstring(fetch(url))
    except Exception as exc:
        print("WARN", configured_source["name"], exc)
        continue

    for node in xml_root.findall(".//item"):
        raw_title = clean(node_text(node, ["title"]))
        link = clean(node_text(node, ["link"]))
        published = parse_date(
            node_text(node, ["pubDate", "published", "updated"])
        )
        description = node_text(node, ["description", "summary"])

        if not raw_title or not link or not published or published < CUTOFF:
            continue

        if configured_source["type"] == "google":
            title, publisher = split_google_title(raw_title)
            publisher = normalize_publisher(
                publisher or configured_source["name"]
            )
        else:
            title = raw_title
            publisher = configured_source["name"]

        named_journalist = detect_named_journalist(title, description)
        if named_journalist:
            publisher = named_journalist

        if publisher in BLOCKED_PUBLISHERS:
            continue

        combined = f"{title} {description}"
        if (
            RESULT_WORDS.search(title)
            or JUNK_WORDS.search(combined)
            or LOW_VALUE_TITLE.search(title)
        ):
            continue

        # Official club sites must contain a genuine news signal.
        if publisher in OFFICIAL_NAMES and not NEWS_WORDS.search(combined):
            continue

        score = news_score(
            title,
            description,
            publisher,
            configured_source.get("priority", 50),
        )
        if score < 85:
            continue

        category_list = categories(title)
        items.append(
            {
                "published_at": published.isoformat(),
                "updated_at": published.isoformat(),
                "title": title,
                "summary": short_summary(description, title),
                "category": category_list,
                "status": source_status(publisher, 1),
                "entity": "",
                "entity_type": (
                    "coaches" if "Tränare" in category_list else "players"
                ),
                "image": "",
                "source_priority": score,
                "sources": [{"name": publisher, "url": link}],
            }
        )

items.extend(read_existing())
items.sort(
    key=lambda item: (
        item.get("source_priority", 50),
        item["published_at"],
    ),
    reverse=True,
)

groups: list[list[dict]] = []

for item in items:
    matching_group = None
    for group in groups:
        if similar_story(item, group[0]):
            matching_group = group
            break

    if matching_group is None:
        groups.append([item])
    else:
        matching_group.append(item)

merged: list[dict] = []

for group in groups:
    group.sort(
        key=lambda item: item.get("source_priority", 50),
        reverse=True,
    )
    lead = dict(group[0])

    unique_sources: list[dict] = []
    seen_sources: set[str] = set()

    for item in group:
        for source in item.get("sources", []):
            if source["name"] not in seen_sources:
                seen_sources.add(source["name"])
                unique_sources.append(source)

    lead["sources"] = unique_sources
    lead["status"] = source_status(
        unique_sources[0]["name"],
        len(unique_sources),
    )

    lead["rank_score"] = (
        max(item.get("source_priority", 50) for item in group)
        + min(35, (len(unique_sources) - 1) * 12)
    )

    lead["id"] = re.sub(
        r"[^a-z0-9]+",
        "-",
        normalized(lead["title"]),
    ).strip("-")[:90]

    lead.pop("source_priority", None)
    merged.append(lead)

merged.sort(
    key=lambda item: (
        item["rank_score"],
        item["published_at"],
    ),
    reverse=True,
)

# Source diversity: prevent one publisher from filling the entire page.
selected: list[dict] = []
publisher_counts: defaultdict[str, int] = defaultdict(int)

for item in merged:
    main_source = item["sources"][0]["name"] if item["sources"] else "Okänd"
    limit = 12 if main_source in PRIMARY_SOURCES or main_source in OFFICIAL_NAMES else 5
    if publisher_counts[main_source] >= limit:
        continue
    publisher_counts[main_source] += 1
    item = dict(item)
    item.pop("rank_score", None)
    selected.append(item)
    if len(selected) >= MAX_ITEMS:
        break



def editorial_title(title: str) -> str:
    title = clean(title)
    title = re.sub(
        r"^(Senast|Senaste nytt|På bilder|I bilder|Officiellt|Breaking)\s*[:.!\-–—]*\s*",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(
        r"\s*[\|\-–—]\s*(Official Site|Official Website|News|Latest News).*$",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(r"\bMan Utd\b", "Manchester United", title, flags=re.I)
    title = re.sub(r"\bMan City\b", "Manchester City", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" .-|–—")
    return title


def editorial_summary(summary: str, title: str) -> str:
    summary = clean(summary)
    summary = re.sub(
        r"\b(Reuters|BBC Sport|Sky Sports|The Guardian|ESPN|"
        r"Business Upturn|FootballTransfers|Official Site|Official Website)\b\s*$",
        "",
        summary,
        flags=re.I,
    )
    summary = re.sub(r"\s+", " ", summary).strip()

    # Avoid a summary that merely repeats the headline.
    if normalized(summary) == normalized(title) or len(summary) < 45:
        return f"{title}. Fler detaljer finns hos huvudkällan."

    # Keep a concise editorial brief.
    sentences = re.split(r"(?<=[.!?])\s+", summary)
    chosen = []
    seen = set()
    for sentence in sentences:
        sentence = sentence.strip()
        key = normalized(sentence)[:110]
        if len(sentence) < 25 or key in seen:
            continue
        seen.add(key)
        chosen.append(sentence)
        if len(chosen) >= 4:
            break
    result = " ".join(chosen).strip()
    return result[:900] if result else f"{title}. Fler detaljer finns hos huvudkällan."


def enrich_sources(item: dict) -> dict:
    item = dict(item)
    sources = item.get("sources", [])
    item["main_source"] = sources[0] if sources else None
    item["confirmed_by"] = sources[1:] if len(sources) > 1 else []
    return item


def looks_swedish(text: str) -> bool:
    common = re.compile(
        r"\b(och|att|har|är|för|med|från|till|inte|som|klubben|spelaren|"
        r"tränaren|övergången|uppges|enligt|bekräftar|avtal|värvning)\b",
        re.I,
    )
    return bool(common.search(text))


def google_translate(text: str, timeout: int = 12) -> str:
    if not text or looks_swedish(text):
        return text

    query = urllib.parse.urlencode(
        {
            "client": "gtx",
            "sl": "auto",
            "tl": "sv",
            "dt": "t",
            "q": text,
        }
    )
    url = "https://translate.googleapis.com/translate_a/single?" + query
    request = urllib.request.Request(url, headers={"User-Agent": UA})

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=ssl.create_default_context(),
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
        translated = "".join(
            part[0] for part in data[0] if part and part[0]
        ).strip()
        return translated or text
    except Exception as exc:
        print("WARN translation", exc)
        return text


def translate_item(item: dict) -> dict:
    item = dict(item)
    separator = "\n---FOTBOLLSBRIEFEN---\n"
    combined = f"{item['title']}{separator}{item['summary']}"
    translated = google_translate(combined)

    if separator in translated:
        title, summary = translated.split(separator, 1)
        item["title"] = title.strip()
        item["summary"] = summary.strip()
    else:
        # Some translation responses alter punctuation around separators.
        parts = re.split(
            r"\s*-{2,}\s*FOTBOLLSBRIEFEN\s*-{2,}\s*",
            translated,
            maxsplit=1,
            flags=re.I,
        )
        if len(parts) == 2:
            item["title"] = parts[0].strip()
            item["summary"] = parts[1].strip()
        else:
            item["title"] = google_translate(item["title"])
            item["summary"] = google_translate(item["summary"])

    replacements = {
        "Man Utd": "Manchester United",
        "Man City": "Manchester City",
        "£": "£",
    }
    for old, new in replacements.items():
        item["title"] = item["title"].replace(old, new)
        item["summary"] = item["summary"].replace(old, new)

    item["title"] = editorial_title(item["title"])
    item["summary"] = editorial_summary(item["summary"], item["title"])
    item["language"] = "sv"
    return enrich_sources(item)


def translate_items(items: list[dict]) -> list[dict]:
    if not items:
        return []

    translated: list[dict | None] = [None] * len(items)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(translate_item, item): index
            for index, item in enumerate(items)
        }
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                translated[index] = future.result()
            except Exception as exc:
                print("WARN item translation", exc)
                translated[index] = enrich_sources(items[index])

    return [
        item if item is not None else items[index]
        for index, item in enumerate(translated)
    ]


if not selected:
    print("No acceptable news found; keeping existing news.json unchanged")
    raise SystemExit(0)

print("Translating", len(selected), "items to Swedish")
selected = translate_items(selected)
selected = [
    item for item in selected
    if item.get("title")
    and not re.search(
        r"\b(officiella hemsida|official website|club website|på bilder|i bilder|presskonferens)\b",
        item["title"],
        re.I,
    )
]
selected = selected[:60]

payload = {
    "updated_at": NOW.isoformat(),
    "items": selected,
}

OUT.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(
    "Wrote",
    len(selected),
    "items from",
    len({s["name"] for item in selected for s in item["sources"]}),
    "sources",
)
