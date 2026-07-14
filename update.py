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
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "sources.json"
OUTPUT_PATH = ROOT / "news.json"
USER_AGENT = "Mozilla/5.0 Fotbollsbriefen/EnglishOnly-1.0"
NOW = dt.datetime.now(dt.timezone.utc)

JUNK_RE = re.compile(
    r"\b(shop|store|shirt|jersey|kit|merchandise|tickets?|hospitality|gallery|"
    r"training gallery|inside training|watch now|video|quiz|wallpaper|download|"
    r"membership|museum|tour|matchday programme|podcast|fixtures?|kick-?off|"
    r"line-?up|starting xi|team news|live blog|live updates|academy|under-18|u18|"
    r"under-21|u21|women'?s|kids?|press conference|best of|how to watch|preview|"
    r"highlights|match report|standings|table|full-time|final score)\b",
    re.I,
)

NEWS_SIGNAL_RE = re.compile(
    r"\b(sign|signs|signed|transfer|joins|joined|appoint|appointed|sack|sacked|"
    r"leaves|left|contract|renew|extension|injury|injured|operation|surgery|"
    r"director|manager|coach|ban|suspension|investigation|fine|takeover|ownership|"
    r"bid|agreement|deal|medical|loan|release clause|retire|retirement|statement|"
    r"confirmed|official|agrees|talks|negotiations|offer|rejects?|accepts?)\b",
    re.I,
)

LOW_VALUE_TITLE_RE = re.compile(
    r"\b(transfer news today|latest transfer news|round-up|daily round-up|"
    r"what we learned|five things|everything you need to know|explained|"
    r"all you need to know|official website|club website|homepage)\b",
    re.I,
)

LIVE_AND_GOSSIP_RE = re.compile(
    r"\b(transfer centre live|transfer live|live transfer|deadline day live|"
    r"live blog|rumour mill|rumor mill|gossip|latest rumours?|latest rumors?|"
    r"transfer updates and rumours|transfer updates and rumors)\b",
    re.I,
)

CLUB_MARKETING_RE = re.compile(
    r"\b(supporters? club|fan club|membership|hospitality|tickets?|shop|store|"
    r"merchandise|museum|stadium tour|community programme|community program|"
    r"charity|foundation|meet the fans|we love you|official supporters?)\b",
    re.I,
)

CLICKBAIT_RE = re.compile(
    r"\b(iconic|stunning|amazing|fantastic|incredible|shock|shocking|"
    r"could change everything|you won't believe)\b",
    re.I,
)

NON_NEWS_PROFILE_RE = re.compile(
    r"\b(here we go.*phrase|phrase happened by accident|origin of.*phrase|"
    r"meet the journalist|inside the life of|journey to becoming)\b",
    re.I,
)

OFF_TOPIC_RE = re.compile(
    r"\b(crypto-backed|cryptocurrency|crypto market|nft|web3|"
    r"picks two midfielders to keep|picks .* to keep at)\b",
    re.I,
)

TRUSTED_PUBLISHERS = {
    "Reuters", "BBC Sport", "Simon Stone / BBC Sport",
    "David Ornstein / The Athletic", "The Athletic", "Fabrizio Romano",
    "Gianluca Di Marzio", "Sky Sports", "Kicker", "L'Équipe",
    "RMC Sport", "The Guardian", "ESPN", "Football Italia",
    "Marca", "AS", "Mundo Deportivo",
}

BLOCKED_PUBLISHERS = {
    "Business Upturn", "FootballTransfers", "Sports Mole", "CaughtOffside",
    "TEAMtalk", "Transfer Tavern", "Football365", "OneFootball", "Tribuna.com",
    "Opinion Nigeria", "Daily Post Nigeria", "Crypto Briefing",
    "Football FanCast", "GiveMeSport", "EPL Index", "The Hard Tackle",
    "The Peoples Person", "Football Insider", "Soccer Laduma", "Sportskeeda",
    "The Sun", "Daily Mail", "Mirror", "Express",
}

SOURCE_ALIASES = {
    "BBC": "BBC Sport",
    "BBC.com": "BBC Sport",
    "Reuters.com": "Reuters",
    "The Athletic - The New York Times": "The Athletic",
    "The New York Times": "The Athletic",
    "Sky Sports Football": "Sky Sports",
    "GianlucaDiMarzio.com": "Gianluca Di Marzio",
    "Official Manchester United Website": "Manchester United",
    "Chelsea Football Club": "Chelsea",
    "Manchester City FC": "Manchester City",
    "Liverpool FC": "Liverpool",
    "Arsenal.com": "Arsenal",
    "Juventus.com": "Juventus",
}

SOURCE_RANK = {
    "Reuters": 105,
    "BBC Sport": 104,
    "Simon Stone / BBC Sport": 104,
    "David Ornstein / The Athletic": 103,
    "The Athletic": 102,
    "Fabrizio Romano": 101,
    "Sky Sports": 95,
    "The Guardian": 91,
    "ESPN": 89,
    "Football Italia": 88,
}

OFFICIAL_SOURCES = {
    "Manchester United", "Manchester City", "Liverpool", "Arsenal", "Chelsea",
    "Tottenham Hotspur", "Newcastle United", "Aston Villa", "Barcelona",
    "Real Madrid", "Inter", "AC Milan", "Juventus",
    "Bayern Munich", "Borussia Dortmund", "Bayer Leverkusen",
 "Monaco",
}

DOMAIN_RE = re.compile(
    r"(?:\s|^)(?:[A-Za-z0-9-]+\.)+(?:com|net|org|co\.uk|it|fr|de|es|se)\b",
    re.I,
)

SOURCE_RESIDUE_RE = re.compile(
    r"\b(?:Reuters|BBC Sport|Sky Sports|The Athletic|Football Italia|ESPN|"
    r"The Guardian|L'Équipe|RMC Sport|Marca|AS|Mundo Deportivo|Official Site|"
    r"Official Website|Latest News|News)\b\s*$",
    re.I,
)

STOPWORDS = {
    "the", "and", "for", "from", "with", "into", "over", "after", "before",
    "amid", "about", "near", "close", "sign", "signs", "signed", "transfer",
    "news", "official", "football", "club", "deal", "move", "talks", "report",
    "reports", "latest", "today", "why", "what", "how", "when", "this", "that",
}


def fetch(url: str, timeout: int = 22) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(
        request,
        timeout=timeout,
        context=ssl.create_default_context(),
    ) as response:
        return response.read()


def clean_text(value: str) -> str:
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
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
        except Exception:
            return None


def normalize(value: str) -> str:
    value = re.sub(r"[^a-z0-9 ]", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


def significant_words(value: str) -> set[str]:
    return {
        word for word in re.findall(r"[a-z0-9]+", normalize(value))
        if len(word) >= 4 and word not in STOPWORDS
    }


def similarity(first: str, second: str) -> float:
    if not first or not second:
        return 0.0
    return SequenceMatcher(None, normalize(first), normalize(second)).ratio()


def node_text(node: ET.Element, names: list[str]) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def google_news_url(query: str, days: int) -> str:
    query = f"{query} when:{days}d"
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"}
    )


def split_google_title(raw_title: str) -> tuple[str, str | None]:
    if " - " not in raw_title:
        return raw_title.strip(), None
    title, publisher = raw_title.rsplit(" - ", 1)
    return title.strip(), publisher.strip()


def clean_headline(title: str) -> str:
    title = clean_text(title)
    title = re.sub(
        r"\s*[\|\-–—]\s*(Official Site|Official Website|News|Latest News).*$",
        "",
        title,
        flags=re.I,
    )
    return re.sub(r"\s+", " ", title).strip(" .-|–—")


def clean_excerpt(summary: str, title: str) -> str:
    summary = clean_text(summary)
    summary = DOMAIN_RE.sub(" ", summary)
    summary = SOURCE_RESIDUE_RE.sub("", summary).strip(" .-|–—")
    if similarity(title, summary) >= 0.86:
        return ""

    sentences: list[str] = []
    seen: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+", summary):
        sentence = clean_text(sentence)
        if len(sentence) < 35 or similarity(title, sentence) >= 0.86:
            continue
        key = normalize(sentence)
        if not key or key in seen:
            continue
        seen.add(key)
        sentences.append(sentence)
        if len(sentences) >= 3:
            break
    return " ".join(sentences)[:850]


def categories(title: str) -> list[str]:
    lowered = title.lower()
    output: list[str] = []
    pairs = [
        ("Premier League", r"manchester|liverpool|arsenal|chelsea|tottenham|newcastle|aston villa|premier league|wolves|west ham|everton|crystal palace"),
        ("La Liga", r"barcelona|real madrid|atl[eé]tico|la liga|sevilla|villarreal|valencia|celta"),
        ("Serie A", r"juventus|milan|inter|napoli|roma|lazio|atalanta|fiorentina|serie a"),
        ("Bundesliga", r"bayern|dortmund|leverkusen|bundesliga|leipzig|frankfurt"),
        ("Ligue 1", r"psg|paris saint|marseille|monaco|lyon|ligue 1"),
        ("Transfer", r"transfer|sign|deal|bid|joins|move|contract|renew|loan|medical|talks|agreement"),
        ("Coach", r"manager|coach|head coach|trainer|sack|appointed"),
        ("Club management", r"sporting director|director of football|chief executive|ceo|owner|ownership"),
    ]
    for category, pattern in pairs:
        if re.search(pattern, lowered):
            output.append(category)
    return output or ["Football"]


def source_status(source: str, source_count: int) -> str:
    if source in OFFICIAL_SOURCES:
        return "Official"
    if source_count >= 2:
        return "Confirmed by multiple sources"
    if source in {"Reuters", "BBC Sport", "Simon Stone / BBC Sport"}:
        return "Confirmed"
    if source in {
        "David Ornstein / The Athletic",
        "Fabrizio Romano",
            "The Athletic",
    }:
        return "Highly credible"
    return "Reported"


def normalized_source(name: str) -> str:
    return SOURCE_ALIASES.get(name.strip(), name.strip())


def source_score(name: str, configured_priority: int) -> int:
    name = normalized_source(name)
    if name in SOURCE_RANK:
        return SOURCE_RANK[name]
    if name in OFFICIAL_SOURCES:
        return 90
    return configured_priority


def rank_sources(sources: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: dict[str, dict[str, str]] = {}
    for source in sources:
        name = normalized_source(source.get("name", ""))
        if not name or name in BLOCKED_PUBLISHERS:
            continue
        if name not in unique:
            unique[name] = {"name": name, "url": source.get("url", "")}
    return sorted(
        unique.values(),
        key=lambda source: source_score(source["name"], 50),
        reverse=True,
    )



def quality_score(
    title: str,
    description: str,
    publisher: str,
    configured_priority: int,
) -> int:
    combined = f"{title} {description}"
    score = source_score(publisher, configured_priority)
    if publisher in TRUSTED_PUBLISHERS or publisher in OFFICIAL_SOURCES:
        score += 25
    if NEWS_SIGNAL_RE.search(combined):
        score += 20
    if publisher in OFFICIAL_SOURCES and NEWS_SIGNAL_RE.search(combined):
        score += 10
    if JUNK_RE.search(combined):
        score -= 150
    if LOW_VALUE_TITLE_RE.search(title):
        score -= 100
    if LIVE_AND_GOSSIP_RE.search(title):
        score -= 180
    if CLUB_MARKETING_RE.search(combined):
        score -= 180
    if CLICKBAIT_RE.search(title) and publisher not in TRUSTED_PUBLISHERS and publisher not in OFFICIAL_SOURCES:
        score -= 70
    if publisher not in TRUSTED_PUBLISHERS and publisher not in OFFICIAL_SOURCES:
        score -= 25
    if len(clean_text(description)) < 45:
        score -= 15
    return score



NON_ENGLISH_WORDS = {
    # Italian
    "della", "degli", "delle", "alla", "allo", "agli", "nella", "nello",
    "mercato", "calciomercato", "arriva", "arrivano", "giocatore", "allenatore",
    "squadra", "ufficiale", "contratto", "infortunio", "prestito", "cessione",
    # French
    "avec", "pour", "chez", "dans", "mercato", "entraîneur", "entraineur",
    "joueur", "équipe", "equipe", "blessure", "contrat", "prêt", "pret",
    # Spanish
    "fichaje", "fichajes", "entrenador", "jugador", "equipo", "lesion",
    "lesión", "contrato", "cesion", "cesión", "llega", "llegan",
    # German
    "spieler", "trainer", "vertrag", "verletzung", "wechsel", "verpflichtet",
    "mannschaft", "bundestrainer",
}

NON_ENGLISH_FUNCTION_WORDS = {
    "della", "degli", "delle", "nella", "nello", "agli", "allo",
    "avec", "pour", "chez", "dans", "des", "les",
    "para", "desde", "hasta", "tambien", "también",
    "der", "die", "das", "ein", "eine", "einen", "einem", "einer", "und", "für",
}


def is_probably_english(title: str, description: str = "") -> bool:
    text = clean_text(f"{title} {description}").lower()
    words = re.findall(r"[a-zà-öø-ÿ]+", text)

    strong_hits = sum(word in NON_ENGLISH_WORDS for word in words)
    function_hits = sum(word in NON_ENGLISH_FUNCTION_WORDS for word in words)

    # One unmistakable football term or several foreign function words is enough.
    if strong_hits >= 1:
        return False
    if function_hits >= 3:
        return False

    return True


def parse_feed(xml_bytes: bytes, source: dict[str, Any], cutoff: dt.datetime) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    output: list[dict[str, Any]] = []

    for node in root.findall(".//item"):
        raw_title = clean_text(node_text(node, ["title"]))
        link = clean_text(node_text(node, ["link"]))
        published = parse_date(node_text(node, ["pubDate", "published", "updated"]))
        raw_description = node_text(node, ["description", "summary"])

        if not raw_title or not link or not published or published < cutoff:
            continue

        if source["type"] == "google":
            title, publisher = split_google_title(raw_title)
            publisher = normalized_source(publisher or source["name"])
        else:
            title = raw_title
            publisher = normalized_source(source["name"])

        title = clean_headline(title)
        combined = f"{title} {raw_description}"

        if not is_probably_english(title, raw_description):
            continue
        if publisher in BLOCKED_PUBLISHERS:
            continue
        if JUNK_RE.search(combined) or LOW_VALUE_TITLE_RE.search(title):
            continue
        if LIVE_AND_GOSSIP_RE.search(title):
            continue
        if CLUB_MARKETING_RE.search(combined):
            continue
        if NON_NEWS_PROFILE_RE.search(combined):
            continue
        if OFF_TOPIC_RE.search(combined):
            continue
        if publisher in OFFICIAL_SOURCES and not NEWS_SIGNAL_RE.search(combined):
            continue

        score = quality_score(
            title,
            raw_description,
            publisher,
            int(source.get("priority", 50)),
        )
        if score < 95:
            continue

        summary = clean_excerpt(raw_description, title)
        cats = categories(title)

        output.append({
            "published_at": published.isoformat(),
            "updated_at": published.isoformat(),
            "title": title,
            "summary": summary,
            "category": cats,
            "status": source_status(publisher, 1),
            "entity": "",
            "entity_type": "coaches" if "Coach" in cats else "players",
            "club": "",
            "image": "",
            "source_priority": score,
            "sources": [{"name": publisher, "url": link}],
            "language": "en",
        })

    return output


def same_story(first: dict[str, Any], second: dict[str, Any]) -> bool:
    ratio = similarity(first["title"], second["title"])
    if ratio >= 0.72:
        return True

    first_words = significant_words(first["title"])
    second_words = significant_words(second["title"])
    overlap = first_words & second_words
    return len(overlap) >= 4


def merge_stories(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items.sort(
        key=lambda item: (item.get("source_priority", 50), item["published_at"]),
        reverse=True,
    )

    groups: list[list[dict[str, Any]]] = []
    for item in items:
        matching: list[dict[str, Any]] | None = None
        for group in groups:
            if same_story(item, group[0]):
                matching = group
                break
        if matching is None:
            groups.append([item])
        else:
            matching.append(item)

    merged: list[dict[str, Any]] = []
    for group in groups:
        group.sort(key=lambda item: item.get("source_priority", 50), reverse=True)
        lead = dict(group[0])

        all_sources: list[dict[str, str]] = []
        for item in group:
            all_sources.extend(item.get("sources", []))
        ranked_sources = rank_sources(all_sources)
        if not ranked_sources:
            continue

        lead["sources"] = ranked_sources
        lead["main_source"] = ranked_sources[0]
        lead["confirmed_by"] = ranked_sources[1:5]
        lead["status"] = source_status(ranked_sources[0]["name"], len(ranked_sources))
        lead["rank_score"] = max(item.get("source_priority", 50) for item in group) + min(30, (len(ranked_sources) - 1) * 10)
        lead["id"] = re.sub(r"[^a-z0-9]+", "-", normalize(lead["title"])).strip("-")[:90]

        if not lead.get("summary"):
            lead["summary"] = (
                "Read the full report from the main source."
            )

        lead.pop("source_priority", None)
        merged.append(lead)

    merged.sort(
        key=lambda item: (item["rank_score"], item["published_at"]),
        reverse=True,
    )
    return merged


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cutoff = NOW - dt.timedelta(days=int(config.get("max_age_days", 7)))

    collected: list[dict[str, Any]] = []
    for source in config["feeds"]:
        url = source["url"] if source["type"] == "rss" else google_news_url(
            source["query"],
            int(config.get("max_age_days", 7)),
        )
        try:
            collected.extend(parse_feed(fetch(url), source, cutoff))
        except Exception as exc:
            print(f"WARN {source['name']}: {exc}")

    if not collected:
        print("No fresh stories fetched; keeping the existing news.json unchanged.")
        return 0

    merged = merge_stories(collected)

    selected: list[dict[str, Any]] = []
    publisher_counts: dict[str, int] = {}
    max_items = int(config.get("max_items", 45))

    for item in merged:
        source_name = item["main_source"]["name"]
        limit = 10 if source_name in SOURCE_RANK or source_name in OFFICIAL_SOURCES else 4
        if publisher_counts.get(source_name, 0) >= limit:
            continue
        publisher_counts[source_name] = publisher_counts.get(source_name, 0) + 1

        item = dict(item)
        item.pop("rank_score", None)
        selected.append(item)
        if len(selected) >= max_items:
            break

    if not selected:
        print("No acceptable stories remained; keeping the existing news.json unchanged.")
        return 0

    payload = {"updated_at": NOW.isoformat(), "items": selected}
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(selected)} stories from {len({s['name'] for i in selected for s in i['sources']})} sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
