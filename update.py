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
from html.parser import HTMLParser
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "sources.json"
OUTPUT_PATH = ROOT / "news.json"
USER_AGENT = "Mozilla/5.0 Fotbollsbriefen/ArticleIntro-1.1"
NOW = dt.datetime.now(dt.timezone.utc)

# Enkel ordlista för att koppla nyheter till spelare/tränare och klubbar för bildarkivet
FAMOUS_ENTITIES = {
    "Klopp": ("jurgen-klopp", "coaches"),
    "Alonso": ("xabi-alonso", "coaches"),
    "Trossard": ("leandro-trossard", "players"),
    "Vuskovic": ("luka-vuskovic", "players"),
    "Baum": ("lisa-baum", "players"),
    "Balogun": ("folarin-balogun", "players"),
    "Tielemans": ("youri-tielemans", "players"),
    "Guardiola": ("pep-guardiola", "coaches"),
    "Arteta": ("mikel-arteta", "coaches"),
    "Haaland": ("erling-haaland", "players"),
    "Mbappe": ("kylian-mbappe", "players"),
    "Messi": ("lionel-messi", "players"),
    "Maradona": ("diego-maradona", "players"),
}

FAMOUS_CLUBS = {
    "Arsenal": "arsenal",
    "Man Utd": "manchester-united",
    "Manchester United": "manchester-united",
    "Man City": "manchester-city",
    "Manchester City": "manchester-city",
    "Liverpool": "liverpool",
    "Chelsea": "chelsea",
    "Tottenham": "tottenham",
    "Spurs": "tottenham",
    "Brighton": "brighton",
    "Everton": "everton",
    "Real Madrid": "real-madrid",
    "Barcelona": "barcelona",
    "Bayern": "bayern-munich",
    "Dortmund": "borussia-dortmund",
    "Paris Saint-Germain": "psg",
    "PSG": "psg",
}

# Utökad för att stoppa kändis-/livsstilsartiklar utan sportsligt nyhetsvärde
JUNK_RE = re.compile(
    r"\b(shop|store|shirt|jersey|kit|merchandise|tickets?|hospitality|gallery|"
    r"training gallery|inside training|watch now|video|quiz|wallpaper|download|"
    r"membership|museum|tour|matchday programme|podcast|fixtures?|kick-?off|"
    r"line-?up|starting xi|team news|live blog|live updates|academy|under-18|u18|"
    r"under-21|u21|women'?s|kids?|press conference|best of|how to watch|preview|"
    r"highlights|match report|standings|table|full-time|final score|"
    r"hair-stylist|haircut|barber|frisör|frisyr|fashion|style|lifestyle)\b",
    re.I,
)

# Utökad för att blockera IOK, OS-politik och icke-fotbollsrelaterade organisationsnyheter
OTHER_SPORTS_RE = re.compile(
    r"\b(crick|golf|mcilroy|tiger woods|formula 1|f1|tennis|wimbledon|djokovic|"
    r"alcaraz|swimming|athlet|rugby|nfl|super bowl|nba|basket|baseball|"
    r"boxning|boxing|ufc|t20|test match|ashes|ryder cup|olympic|olympiska|"
    r"horse racing|jockey|dettori|newmarket|ascot|cheltenham|grand national|"
    r"equestrian|showjumping|dressage|ridsport|"
    r"skid|längdskid|skidskytte|alpin|slalom|frida karlsson|ebba andersson|kalla|"
    r"jonna sundling|shiffrin|victoriapris|victoriastipend|friidrott|löpning|"
    r"stavhopp|duplantis|ståhl|diskus|höjdhopp|ishockey|hockey|shl|nhl|"
    r"shubman gill|julien alfred|tharp|axar patel|odi|häck|edgbaston|stands|"
    r"iok|ioc|olympiska kommitt|eu-stöd|sportpolitik)\w*",
    re.I,
)

NEWS_SIGNAL_RE = re.compile(
    r"\b(sign|signs|signed|transfer|joins|joined|appoint|appointed|sack|sacked|"
    r"leaves|left|contract|renew|extension|injury|injured|operation|surgery|"
    r"director|manager|coach|ban|suspension|investigation|fine|takeover|ownership|"
    r"bid|agreement|deal|medical|loan|release clause|retire|retirement|statement|"
    r"confirmed|official|agrees|talks|negotiations|offer|rejects?|accepts?|"
    r"värvning|klar för|kontrakt|skada|skadad|förlänger|tränare|sparkas|säljs)\b",
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

# Skadenyheter — används för att tagga kategorin "Injury" oavsett liga
INJURY_RE = re.compile(
    r"\b(injury|injuries|injured|surgery|operation|scan|sidelined|"
    r"out for weeks|out for months|hamstring|groin injury|knee injury|"
    r"ankle injury|muscle injury|knee surgery|\bacl\b|fitness test|"
    r"medical update|skada|skadad|skadeuppdatering|skadeläge|opereras|"
    r"axelskada|knäskada|vadskada|hälseneskada|korsbandsskada|"
    r"ryggskada|ljumskskada)\b",
    re.I,
)

TRUSTED_PUBLISHERS = {
    "Reuters", "BBC Sport", "Simon Stone / BBC Sport",
    "David Ornstein / The Athletic", "The Athletic", "Fabrizio Romano",
    "Gianluca Di Marzio", "Sky Sports", "Kicker", "L'Équipe",
    "RMC Sport", "The Guardian", "ESPN", "Football Italia",
    "Marca", "AS", "Mundo Deportivo",
    "Paul Joyce", "Sam Lee", "Laurie Whitwell", "James Pearce", "Guillem Balagué",
    "SVT Sport", "SvenskaFans", "Fotbolltransfers", "Fotbollskanalen"
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
    "Paul Joyce": 103,
    "The Athletic": 102,
    "SVT Sport": 102,
    "Fabrizio Romano": 101,
    "Gianluca Di Marzio": 101,
    "Sam Lee": 100,
    "Laurie Whitwell": 100,
    "James Pearce": 100,
    "Guillem Balagué": 99,
    "Fotbolltransfers": 96,
    "Sky Sports": 95,
    "Fotbollskanalen": 93,
    "The Guardian": 91,
    "SvenskaFans": 90,
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

SWEDISH_WORDS = {"och", "att", "en", "ett", "med", "om", "eller", "men", "hos", "till", "från", "av", "på", "i"}


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
    value = re.sub(r"[^a-zåäö0-9 ]", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


def significant_words(value: str) -> set[str]:
    return {
        word for word in re.findall(r"[a-zåäö0-9]+", normalize(value))
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
        {"q": query, "hl": "sv-SE", "gl": "SE", "ceid": "SE:sv"} if "fotbollskanalen" in query else {"q": query, "hl": "en-GB", "gl": "GB", "ceid": "GB:en"}
    )


def split_google_title(raw_title: str) -> tuple[str, str | None]:
    if " - " not in raw_title:
        return raw_title.strip(), None
    title, publisher = raw_title.rsplit(" - ", 1)
    return title.strip(), publisher.strip()


def clean_headline(title: str) -> str:
    title = clean_text(title)
    title = re.sub(
        r"\s*[\|\-–—]\s*(Official Site|Official Website|News|Latest News|SVT Sport|Fotbollskanalen|SvenskaFans).*$",
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


def categories(title: str, publisher: str = "") -> list[str]:
    lowered = title.lower()
    output: list[str] = []

    if publisher in {"SVT Sport", "SvenskaFans", "Fotbolltransfers", "Fotbollskanalen"}:
        output.append("Sverige")

    pairs = [
        ("Premier League", r"manchester|liverpool|arsenal|chelsea|tottenham|newcastle|aston villa|premier league|wolves|west ham|everton|crystal palace"),
        ("La Liga", r"barcelona|real madrid|atl[eé]tico|la liga|sevilla|villarreal|valencia|celta"),
        ("Serie A", r"juventus|milan|inter|napoli|roma|lazio|atalanta|fiorentina|serie a"),
        ("Bundesliga", r"bayern|dortmund|leverkusen|bundesliga|leipzig|frankfurt"),
        ("Ligue 1", r"psg|paris saint|marseille|monaco|lyon|ligue 1"),
        ("Transfer", r"transfer|sign|deal|bid|joins|move|contract|renew|loan|medical|talks|agreement|värvning|övergång"),
        ("Coach", r"manager|coach|head coach|trainer|sack|appointed|tränare|sparkas"),
        ("Club management", r"sporting director|director of football|chief executive|ceo|owner|ownership"),
    ]
    for category, pattern in pairs:
        if re.search(pattern, lowered):
            output.append(category)

    # Tagga skadenyheter separat, utöver liga/lagtaggning ovan
    if INJURY_RE.search(lowered):
        output.append("Injury")

    return output or (["Sverige"] if "Sverige" in output else ["Football"])


def source_status(source: str, source_count: int) -> str:
    if source in OFFICIAL_SOURCES:
        return "Official"
    if source_count >= 2:
        return "Confirmed by multiple sources"
    if source in {"Reuters", "BBC Sport", "Simon Stone / BBC Sport", "SVT Sport"}:
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
    if OTHER_SPORTS_RE.search(combined):
        score -= 200
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
    "della", "degli", "delle", "alla", "allo", "agli", "nella", "nello",
    "calciomercato", "arriva", "arrivano", "giocatore", "allenatore",
    "squadra", "ufficiale", "contratto", "infortunio", "prestito", "cessione",
    "avec", "pour", "chez", "dans", "entraîneur", "entraineur",
    "joueur", "équipe", "equipe", "blessure", "contrat", "prêt", "pret",
    "fichaje", "fichajes", "entrenador", "jugador", "equipo", "lesion",
    "lesión", "contrato", "cesion", "cesión", "llega", "llegan",
    "spieler", "vertrag", "verletzung", "wechsel", "verpflichtet",
    "mannschaft", "bundestrainer",
}

NON_ENGLISH_FUNCTION_WORDS = {
    "della", "degli", "delle", "nella", "nello", "agli", "allo",
    "avec", "pour", "chez", "dans", "des", "les",
    "para", "desde", "hasta", "tambien", "también",
    "der", "die", "das", "ein", "eine", "einen", "einem", "einer", "und", "für",
}


def is_acceptable_language(title: str, description: str = "") -> bool:
    text = clean_text(f"{title} {description}").lower()
    words = re.findall(r"[a-zåäöà-öø-ÿ]+", text)

    swedish_hits = sum(word in SWEDISH_WORDS for word in words)
    if swedish_hits >= 2:
        return True

    strong_hits = sum(word in NON_ENGLISH_WORDS for word in words)
    function_hits = sum(word in NON_ENGLISH_FUNCTION_WORDS for word in words)

    if strong_hits >= 1 or function_hits >= 3:
        return False

    return True


ARTICLE_BLOCKLIST_RE = re.compile(
    r"\b(cookie|privacy|newsletter|subscribe|sign up|advertisement|"
    r"related articles?|read more|follow us|share this article|"
    r"all rights reserved|terms and conditions|accept all|manage preferences)\b",
    re.I,
)

ARTICLE_BOILERPLATE_RE = re.compile(
    r"^(home|football|sport|sports|news|latest|menu|search|live|"
    r"advertisement|skip to content)$",
    re.I,
)


class ParagraphExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside_p = False
        self._inside_ignored = 0
        self._buffer: list[str] = []
        self.paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "nav", "footer", "header", "aside", "form", "noscript"}:
            self._inside_ignored += 1
            return
        if self._inside_ignored:
            return
        if tag == "p":
            self._inside_p = True
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "nav", "footer", "header", "aside", "form", "noscript"}:
            if self._inside_ignored:
                self._inside_ignored -= 1
            return
        if self._inside_ignored:
            return
        if tag == "p" and self._inside_p:
            text = clean_text(" ".join(self._buffer))
            if text:
                self.paragraphs.append(text)
            self._inside_p = False
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._inside_p and not self._inside_ignored:
            self._buffer.append(data)


class MetaExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        attr = {k.lower(): (v or "") for k, v in attrs}
        key = (attr.get("property") or attr.get("name") or "").lower()
        content = attr.get("content", "").strip()
        if key and content and key not in self.meta:
            self.meta[key] = content


def resolve_article_url(url: str) -> str:
    if "news.google.com" not in url:
        return url
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=15, context=ssl.create_default_context()) as response:
            final = response.geturl()
            if "news.google.com" not in final:
                return final
            page = response.read().decode("utf-8", errors="replace")
    except Exception:
        return url

    for pattern in (
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\'](https?://[^"\']+)',
        r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\'](https?://[^"\']+)',
        r'data-n-au=["\'](https?://[^"\']+)',
        r'href=["\'](https?://(?!news\.google\.com|www\.google\.com|policies\.google\.com|support\.google\.com)[^"\']+)',
    ):
        match = re.search(pattern, page, re.I)
        if match and "news.google.com" not in match.group(1):
            return match.group(1)
    return url


def usable_article_paragraph(text: str, title: str) -> bool:
    text = clean_text(text)
    if len(text) < 70 or len(text) > 900:
        return False
    if ARTICLE_BLOCKLIST_RE.search(text):
        return False
    if ARTICLE_BOILERPLATE_RE.fullmatch(text):
        return False
    if similarity(title, text) >= 0.86:
        return False

    words = text.split()
    if len(words) < 12:
        return False

    if text.count("|") >= 2 or text.count("›") >= 2:
        return False

    return True


def article_meta_description(meta: dict[str, str], title: str) -> str:
    candidate = clean_text(
        meta.get("og:description")
        or meta.get("twitter:description")
        or meta.get("description")
        or ""
    )
    if not candidate:
        return ""
    candidate = SOURCE_RESIDUE_RE.sub("", candidate).strip(" .-|–—")
    if (
        len(candidate) < 60
        or similarity(title, candidate) >= 0.9
        or ARTICLE_BLOCKLIST_RE.search(candidate)
    ):
        return ""
    return candidate[:500]


def extract_article_metadata(article_url: str, title: str) -> tuple[str, str]:
    try:
        if "news.google.com" in article_url:
            return "", ""

        html_bytes = fetch(article_url, timeout=15)
        encoding = "utf-8"
        match = re.search(
            br'charset=["\']?\s*([A-Za-z0-9._-]+)',
            html_bytes[:5000],
            re.I,
        )
        if match:
            encoding = match.group(1).decode("ascii", errors="ignore") or "utf-8"

        page = html_bytes.decode(encoding, errors="replace")

        meta_parser = MetaExtractor()
        meta_parser.feed(page)

        og_image = meta_parser.meta.get("og:image") or ""

        meta_summary = article_meta_description(meta_parser.meta, title)
        if meta_summary:
            return meta_summary, og_image

        parser = ParagraphExtractor()
        parser.feed(page)

        selected: list[str] = []
        seen: set[str] = set()
        for paragraph in parser.paragraphs:
            paragraph = clean_text(paragraph)
            key = normalize(paragraph)
            if not key or key in seen:
                continue
            seen.add(key)

            if not usable_article_paragraph(paragraph, title):
                continue

            selected.append(paragraph)
            if len(" ".join(selected)) >= 220 or len(selected) >= 2:
                break

        return " ".join(selected)[:850], og_image
    except Exception:
        return "", ""


def rss_only_summary(title: str, rss_description: str) -> str:
    rss_summary = clean_excerpt(rss_description, title)
    if rss_summary and len(rss_summary) >= 70:
        return rss_summary

    fragment = clean_text(rss_description)
    fragment = DOMAIN_RE.sub(" ", fragment)
    fragment = SOURCE_RESIDUE_RE.sub("", fragment).strip(" .-|–—")
    if (
        len(fragment) >= 45
        and similarity(title, fragment) < 0.86
        and not ARTICLE_BLOCKLIST_RE.search(fragment)
    ):
        return fragment[:500]
    return ""


def parse_feed(xml_bytes: bytes, source: dict[str, Any], cutoff: dt.datetime) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    output: list[dict[str, Any]] = []

    for node in root.findall(".//item"):
        raw_title = clean_text(node_text(node, ["title"]))
        link = clean_text(node_text(node, ["link"]))
        published = parse_date(node_text(node, ["pubDate", "published", "updated"]))
        raw_description = node_text(node, ["description", "summary"])
        content_encoded = node_text(node, ["{http://purl.org/rss/1.0/modules/content/}encoded"])
        if len(clean_text(content_encoded)) > len(clean_text(raw_description)):
            raw_description = content_encoded

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

        if not is_acceptable_language(title, raw_description):
            continue
        if publisher in BLOCKED_PUBLISHERS:
            continue
        if JUNK_RE.search(combined) or OTHER_SPORTS_RE.search(combined) or LOW_VALUE_TITLE_RE.search(title):
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

        summary = rss_only_summary(title, raw_description)
        cats = categories(title, publisher)

        output.append({
            "published_at": published.isoformat(),
            "updated_at": published.isoformat(),
            "title": title,
            "summary": summary,
            "category": cats,
            "status": source_status(publisher, 1),
            "entity": "",
            "entity_type": "players",
            "club": "",
            "image": "",
            "source_priority": score,
            "sources": [{"name": publisher, "url": link}],
            "language": "en",
        })

    return output


def same_story(first: dict[str, Any], second: dict[str, Any]) -> bool:
    ratio = similarity(first["title"], second["title"])
    if ratio >= 0.62:
        return True

    first_words = significant_words(first["title"])
    second_words = significant_words(second["title"])
    overlap = first_words & second_words

    if len(overlap) >= 3:
        return True

    first_entity = first.get("entity", "")
    second_entity = second.get("entity", "")
    if first_entity and first_entity == second_entity and len(overlap) >= 2:
        return True

    return False


def detect_entities_and_clubs(item: dict[str, Any]) -> None:
    title = item.get("title", "")

    for club_key, club_slug in FAMOUS_CLUBS.items():
        if re.search(rf"\b{re.escape(club_key)}\b", title, re.I):
            item["club"] = club_slug
            break

    for entity_key, (entity_slug, entity_type) in FAMOUS_ENTITIES.items():
        if re.search(rf"\b{re.escape(entity_key)}\b", title, re.I):
            item["entity"] = entity_slug
            item["entity_type"] = entity_type
            break


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

        summary_candidates = [
            clean_text(item.get("summary", ""))
            for item in group
            if clean_text(item.get("summary", ""))
            and clean_text(item.get("summary", "")) != "Read the full report from the main source."
        ]
        if summary_candidates:
            summary_candidates.sort(
                key=lambda text: (
                    similarity(lead["title"], text) < 0.80,
                    len(text),
                ),
                reverse=True,
            )
            lead["summary"] = summary_candidates[0][:850]
        elif not lead.get("summary"):
            lead["summary"] = ""

        lead.pop("source_priority", None)
        merged.append(lead)

    merged.sort(
        key=lambda item: (item["rank_score"], item["published_at"]),
        reverse=True,
    )
    return merged


GENERIC_FALLBACK = "Read the full report from the main source."


def build_fallback(source_name: str) -> str:
    name = (source_name or "").strip()
    if not name:
        return GENERIC_FALLBACK
    if name in OFFICIAL_SOURCES:
        return f"Official update from {name} — full announcement at the source."
    if "/" in name:
        journalist = name.split("/", 1)[0].strip()
        return f"{journalist} has the full story at the source."
    return f"Read the full report at {name}."


def enrich_summary(item: dict[str, Any]) -> None:
    current_summary = clean_text(item.get("summary", ""))
    needs_intro = len(current_summary) < 70

    source = item.get("main_source") or (item.get("sources") or [{}])[0]
    url = source.get("url", "")
    if not url:
        if not current_summary:
            item["summary"] = build_fallback(source.get("name", ""))
        return

    resolved = resolve_article_url(url)
    if resolved and resolved != url:
        source["url"] = resolved
        for other in item.get("sources", []):
            if other.get("url") == url:
                other["url"] = resolved
        url = resolved

    detect_entities_and_clubs(item)

    if needs_intro or not item.get("image"):
        intro, og_image = extract_article_metadata(url, item.get("title", ""))
        if intro and needs_intro:
            item["summary"] = intro
        if og_image:
            item["image"] = og_image

    if not clean_text(item.get("summary", "")):
        item["summary"] = build_fallback(source.get("name", ""))


def enrich_selected(items: list[dict[str, Any]], workers: int = 8) -> None:
    if not items:
        return
    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(enrich_summary, items))


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

    for item in collected:
        detect_entities_and_clubs(item)

    merged = merge_stories(collected)

    selected: list[dict[str, Any]] = []
    publisher_counts: dict[str, int] = {}
    max_items = int(config.get("max_items", 75))

    for item in merged:
        source_name = item["main_source"]["name"]
        if source_name in OFFICIAL_SOURCES:
            limit = 3
        elif source_name in SOURCE_RANK:
            limit = 10
        else:
            limit = 4
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

    enrich_selected(selected)

    payload = {"updated_at": NOW.isoformat(), "items": selected}
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(selected)} stories from {len({s['name'] for i in selected for s in i['sources']})} sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
