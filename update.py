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

UA = "Mozilla/5.0 Fotbollsbriefen/7.5"
NOW = dt.datetime.now(dt.timezone.utc)
CUTOFF = NOW - dt.timedelta(days=CFG.get("max_age_days", 7))
MAX_ITEMS = min(int(CFG.get("max_items", 120)), 55)

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

SOURCE_RANK = {
    "Reuters": 105,
    "BBC Sport": 104,
    "Simon Stone / BBC Sport": 104,
    "David Ornstein / The Athletic": 103,
    "The Athletic": 102,
    "Fabrizio Romano": 101,
    "Gianluca Di Marzio": 100,
    "Sky Sports": 91,
    "Kicker": 90,
    "L'Équipe": 90,
    "RMC Sport": 89,
    "The Guardian": 88,
    "ESPN": 86,
    "Football Italia": 85,
    "Marca": 82,
    "AS": 82,
    "Mundo Deportivo": 80,
}

WEAK_CONFIRMERS = {
    "Yahoo Sports", "Tribuna.com", "Sito Ufficiale", "Official Website",
    "Official Manchester United Website", "Arsenal.com", "footballtransfers.com",
    "Business Upturn", "Sports Mole", "CaughtOffside", "TEAMtalk",
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
    """Keep only recent, Swedish, ordinary stories from the previous file."""
    try:
        payload = json.loads(OUT.read_text(encoding="utf-8"))
        result = []
        for item in payload.get("items", []):
            published = parse_date(item.get("published_at", ""))
            if not published or published < CUTOFF:
                continue
            if item.get("kind") == "briefing":
                continue
            title = clean(item.get("title", ""))
            summary = clean(item.get("summary", ""))
            if not title or not summary:
                continue
            if item.get("language") != "sv" and not (
                looks_swedish(title) and looks_swedish(summary)
            ):
                continue
            item = dict(item)
            item.pop("bullets", None)
            item.pop("kind", None)
            item["source_priority"] = 45
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




CLUB_PATTERNS = [
    ("Manchester United", r"\bManchester United\b|\bMan Utd\b"),
    ("Manchester City", r"\bManchester City\b|\bMan City\b"),
    ("Liverpool", r"\bLiverpool\b"),
    ("Arsenal", r"\bArsenal\b"),
    ("Chelsea", r"\bChelsea\b"),
    ("Tottenham", r"\bTottenham\b|\bSpurs\b"),
    ("Newcastle United", r"\bNewcastle\b"),
    ("Aston Villa", r"\bAston Villa\b"),
    ("Real Madrid", r"\bReal Madrid\b"),
    ("Barcelona", r"\bBarcelona\b|\bBarça\b"),
    ("Atlético Madrid", r"\bAtl[eé]tico Madrid\b"),
    ("Juventus", r"\bJuventus\b"),
    ("Inter", r"\bInter\b"),
    ("AC Milan", r"\bAC Milan\b|\bMilan\b"),
    ("Napoli", r"\bNapoli\b"),
    ("Roma", r"\bRoma\b"),
    ("Lazio", r"\bLazio\b"),
    ("Bayern München", r"\bBayern\b"),
    ("Borussia Dortmund", r"\bDortmund\b"),
    ("Bayer Leverkusen", r"\bLeverkusen\b"),
    ("Paris Saint-Germain", r"\bParis Saint-Germain\b|\bPSG\b"),
    ("Marseille", r"\bMarseille\b"),
    ("Monaco", r"\bMonaco\b"),
]

ROLE_WORDS = {
    "tränare", "manager", "coach", "mittfältare", "anfallare", "försvarare",
    "målvakt", "spelare", "sportchef", "direktör", "president", "ägare",
}


def identify_club(text: str) -> str:
    for club, pattern in CLUB_PATTERNS:
        if re.search(pattern, text, re.I):
            return club
    return ""


def identify_person(title: str) -> str:
    cleaned = editorial_title(title)
    candidates = re.findall(
        r"\b[A-ZÅÄÖÉÜ][a-zåäöéü'-]+(?:\s+[A-ZÅÄÖÉÜ][a-zåäöéü'-]+){1,3}\b",
        cleaned,
    )
    for candidate in candidates:
        words = {word.lower() for word in candidate.split()}
        if words & ROLE_WORDS:
            continue
        if any(club.lower() in candidate.lower() for club, _ in CLUB_PATTERNS):
            continue
        return candidate
    return ""



def official_source_is_primary(item: dict, source_name: str) -> bool:
    title = item.get("title", "")
    summary = item.get("summary", "")
    combined = f"{title} {summary}"

    # Official club sources should lead only when they announce their own action.
    if source_name not in OFFICIAL_NAMES:
        return True

    own_action = re.search(
        r"\b(värvar|värvat|klar för|skriver på|förlänger|förlängt|utser|"
        r"utsett|presenterar|bekräftar|lånar ut|lånar in|lämnar klubben|"
        r"ansluter|appointed|signs|signed|joins|extends|announces|confirms)\b",
        combined,
        re.I,
    )
    external_claim = re.search(
        r"\b(uppges|enligt|ryktas|intresse|jagar|går vidare i jakten|"
        r"close to|set to|linked with|reports suggest)\b",
        combined,
        re.I,
    )
    return bool(own_action and not external_claim)


def source_strength(source: dict, item: dict | None = None) -> int:
    name = source.get("name", "")
    score = SOURCE_RANK.get(name, 92 if name in OFFICIAL_NAMES else 55)
    if item is not None and name in OFFICIAL_NAMES and not official_source_is_primary(item, name):
        score -= 35
    if name in WEAK_CONFIRMERS:
        score -= 50
    return score


def make_editorial_summary(item: dict) -> str:
    title = clean(item.get("title", ""))
    summary = clean(item.get("summary", ""))
    club = item.get("club", "")
    person = item.get("entity", "")
    categories = item.get("category", [])

    summary = re.sub(
        r"\b(Fler detaljer finns hos huvudkällan|Läs mer hos källan|"
        r"Uppgifterna bygger på rapportering från de angivna källorna)\.?$",
        "",
        summary,
        flags=re.I,
    )
    summary = re.sub(
        r"\b(Reuters|BBC Sport|Sky Sports|The Guardian|ESPN|Official Site|"
        r"Official Website|Liverpool FC|Manchester United|Manchester City)\b\s*$",
        "",
        summary,
        flags=re.I,
    ).strip()

    chosen = []
    seen = set()
    for sentence in re.split(r"(?<=[.!?])\s+", summary):
        sentence = sentence.strip()
        key = normalized(sentence)[:130]
        if len(sentence) < 30 or key in seen:
            continue
        if key == normalized(title):
            continue
        if not looks_swedish(sentence):
            continue
        seen.add(key)
        chosen.append(sentence)
        if len(chosen) >= 4:
            break

    if chosen:
        result = " ".join(chosen)
        if len(result) >= 80:
            return result[:900]

    if "Transfer" in categories:
        if person and club:
            return (
                f"{club} uppges arbeta med en övergång för {person}. "
                "Kontakter eller förhandlingar pågår enligt huvudkällan, "
                "men affären är ännu inte officiellt bekräftad."
            )
        if person:
            return (
                f"{person} är aktuell för en övergång. "
                "Fler besked väntas när förhandlingarna har kommit längre."
            )
        if club:
            return (
                f"{club} arbetar med en möjlig övergång. "
                "Affären är ännu inte officiellt bekräftad."
            )

    if "Tränare" in categories:
        if person and club:
            return (
                f"{person} är aktuell för tränaruppdraget i {club}. "
                "Ett officiellt besked har ännu inte lämnats."
            )
        if person:
            return (
                f"{person} är aktuell för ett nytt tränaruppdrag. "
                "Fler besked väntas från de berörda parterna."
            )

    return f"{title}. Fler verifierade detaljer väntas från huvudkällan."


def strongest_source(
    sources: list[dict],
    item: dict | None = None,
) -> tuple[dict | None, list[dict]]:
    if not sources:
        return None, []

    unique = {}
    for source in sources:
        name = source.get("name", "").strip()
        if not name:
            continue
        if name not in unique:
            unique[name] = source

    ranked = sorted(
        unique.values(),
        key=lambda source: source_strength(source, item),
        reverse=True,
    )
    main = ranked[0] if ranked else None
    confirmed = [
        source for source in ranked[1:]
        if source["name"] not in WEAK_CONFIRMERS
    ][:4]
    return main, confirmed


def editorial_synthesis(item: dict) -> dict:
    item = dict(item)
    title = editorial_title(item.get("title", ""))
    summary = editorial_summary(item.get("summary", ""), title)

    club = identify_club(f"{title} {summary}")
    person = identify_person(title)

    # More natural Swedish title patterns.
    title = re.sub(
        r"^(.+?) skriver på för (.+)$",
        lambda m: f"{m.group(1)} klar för {m.group(2)}",
        title,
        flags=re.I,
    )
    title = re.sub(
        r"^(.+?) kommer till (.+?) från (.+)$",
        lambda m: f"{m.group(2)} värvar {m.group(1)} från {m.group(3)}",
        title,
        flags=re.I,
    )
    title = re.sub(
        r"^(.+?) tackar ja till att bli ny (.+)$",
        lambda m: f"{m.group(1)} blir ny {m.group(2)}",
        title,
        flags=re.I,
    )
    title = re.sub(r"\bfår klart grönt ljus för att värva\b", "går vidare i jakten på", title, flags=re.I)
    title = re.sub(r"\benormt pris\b", "högt pris", title, flags=re.I)
    title = re.sub(
        r"^David Ornstein:\s*",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(
        r"^Fabrizio Romano:\s*",
        "",
        title,
        flags=re.I,
    )
    title = re.sub(
        r"^(.+?) värvar före (.+?)$",
        lambda m: f"{m.group(1)} värvar tidigare {m.group(2)}",
        title,
        flags=re.I,
    )
    title = re.sub(
        r"\bEnglands försvarare\b",
        "den engelska försvararen",
        title,
        flags=re.I,
    )

    main, confirmed = strongest_source(item.get("sources", []), item)
    item["title"] = title.strip()
    item["summary"] = make_editorial_summary({
        **item,
        "title": title.strip(),
        "summary": summary.strip(),
        "club": club,
        "entity": person or item.get("entity", ""),
    })
    item["main_source"] = main
    item["confirmed_by"] = confirmed
    item["club"] = club
    item["entity"] = person or item.get("entity", "")
    if person:
        item["entity_type"] = "coaches" if "Tränare" in item.get("category", []) else "players"
    return item


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
    return editorial_synthesis(item)


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

    translated_title = google_translate(item.get("title", ""))
    translated_summary = google_translate(item.get("summary", ""))

    item["title"] = editorial_title(translated_title)
    item["summary"] = clean(translated_summary)

    replacements = {
        "Man Utd": "Manchester United",
        "Man City": "Manchester City",
        "chefstränare": "huvudtränare",
    }
    for old, new in replacements.items():
        item["title"] = item["title"].replace(old, new)
        item["summary"] = item["summary"].replace(old, new)

    item["language"] = "sv"
    return editorial_synthesis(item)


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
    editorial_synthesis(item) for item in selected
    if item.get("title")
    and not re.search(
        r"\b(officiella hemsida|official website|club website|på bilder|i bilder|"
        r"presskonferens|women|damlag|vm-rivaliteten|world cup quarter-finals)\b",
        item["title"],
        re.I,
    )
]
selected = [
    item for item in selected
    if item.get("main_source")
    and item["main_source"].get("name") not in WEAK_CONFIRMERS
    and looks_swedish(item.get("title", ""))
    and looks_swedish(item.get("summary", ""))
]
selected = selected[:55]

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
