#!/usr/bin/env python3
from __future__ import annotations
import datetime as dt, email.utils, html, json, re, ssl, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from difflib import SequenceMatcher

ROOT=Path(__file__).resolve().parent
CFG=json.loads((ROOT/"config/sources.json").read_text(encoding="utf-8"))
OUT=ROOT/"news.json"
UA="Mozilla/5.0 Fotbollsbriefen/2.0"
NOW=dt.datetime.now(dt.timezone.utc)
CUTOFF=NOW-dt.timedelta(days=CFG.get("max_age_days",7))
RESULT_WORDS=re.compile(r"\b(score|result|highlights|standings|table|match report|beat|defeat|draw|full-time|final score|vs)\b",re.I)

def fetch(url,timeout=20):
    req=urllib.request.Request(url,headers={"User-Agent":UA})
    with urllib.request.urlopen(req,timeout=timeout,context=ssl.create_default_context()) as r:
        return r.read()

def text(node, names):
    for name in names:
        x=node.find(name)
        if x is not None and x.text: return x.text.strip()
    return ""

def clean(s):
    s=html.unescape(re.sub(r"<[^>]+>"," ",s or ""))
    return re.sub(r"\s+"," ",s).strip()

def parse_date(s):
    try:
        d=email.utils.parsedate_to_datetime(s)
        if d.tzinfo is None:d=d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        try:return dt.datetime.fromisoformat(s.replace("Z","+00:00")).astimezone(dt.timezone.utc)
        except Exception:return None

def google_url(q):
    q=f'{q} when:{CFG.get("max_age_days",7)}d'
    return "https://news.google.com/rss/search?"+urllib.parse.urlencode({"q":q,"hl":"en-GB","gl":"GB","ceid":"GB:en"})

def category(title):
    t=title.lower(); out=[]
    pairs=[("Premier League",r"manchester|liverpool|arsenal|chelsea|tottenham|newcastle|aston villa|premier league"),
           ("La Liga",r"barcelona|real madrid|atl[eé]tico|la liga"),
           ("Serie A",r"juventus|milan|inter|napoli|roma|lazio|atalanta|serie a"),
           ("Bundesliga",r"bayern|dortmund|leverkusen|bundesliga"),
           ("Ligue 1",r"psg|paris saint|marseille|monaco|ligue 1"),
           ("Transfer",r"transfer|sign|deal|bid|joins|move|contract|renew"),
           ("Tränare",r"manager|coach|head coach|trainer")]
    for c,p in pairs:
        if re.search(p,t):out.append(c)
    return out or ["Fotboll"]

def status(source,title):
    if source in {"Manchester United","Manchester City","Liverpool FC","Arsenal","Chelsea","FC Barcelona","Real Madrid","Atlético Madrid","Inter","AC Milan","Juventus","Bayern München","Borussia Dortmund","Paris Saint-Germain"}:
        return "Officiellt"
    if source in {"Reuters","BBC Sport","Simon Stone / BBC Sport"}: return "Bekräftat"
    if source in {"David Ornstein / The Athletic","Fabrizio Romano","Gianluca Di Marzio"}: return "Mycket trovärdigt"
    return "Rapporterat"

def key(s):
    s=re.sub(r"[^a-z0-9 ]"," ",s.lower())
    return re.sub(r"\s+"," ",s).strip()

def summary(desc,title):
    d=clean(desc)
    if not d:return f"{title}. Läs mer hos källan."
    sentences=re.split(r"(?<=[.!?])\s+",d)
    out=" ".join(sentences[:4]).strip()
    return out[:850]

items=[]
for src in CFG["feeds"]:
    url=src["url"] if src["type"]=="rss" else google_url(src["query"])
    try:
        root=ET.fromstring(fetch(url))
    except Exception as e:
        print("WARN",src["name"],e); continue
    nodes=root.findall(".//item")
    for n in nodes:
        title=clean(text(n,["title"]))
        link=clean(text(n,["link"]))
        pub=parse_date(text(n,["pubDate","published","updated"]))
        desc=text(n,["description","summary"])
        if not title or not link or not pub or pub<CUTOFF or RESULT_WORDS.search(title):continue
        if " - " in title and src["type"]=="google":title=title.rsplit(" - ",1)[0].strip()
        items.append({"published_at":pub.isoformat(),"updated_at":pub.isoformat(),"title":title,"summary":summary(desc,title),
                      "category":category(title),"status":status(src["name"],title),"entity":"","image":"",
                      "source_priority":src.get("priority",50),"sources":[{"name":src["name"],"url":link}]})

items.sort(key=lambda x:(x["source_priority"],x["published_at"]),reverse=True)
groups=[]
for item in items:
    best=None
    for g in groups:
        ratio=SequenceMatcher(None,key(item["title"]),key(g[0]["title"])).ratio()
        if ratio>=0.72:best=g;break
    if best:best.append(item)
    else:groups.append([item])

merged=[]
for g in groups:
    g.sort(key=lambda x:x["source_priority"],reverse=True)
    lead=dict(g[0]); names=set(); sources=[]
    for x in g:
        for s in x["sources"]:
            if s["name"] not in names:names.add(s["name"]);sources.append(s)
    lead["sources"]=sources
    if any(x["status"]=="Officiellt" for x in g):lead["status"]="Officiellt"
    elif len(sources)>1:lead["status"]="Bekräftat av flera källor"
    lead["id"]=re.sub(r"[^a-z0-9]+","-",key(lead["title"])).strip("-")[:90]
    lead.pop("source_priority",None)
    merged.append(lead)

payload={"updated_at":NOW.isoformat(),"items":merged[:CFG.get("max_items",90)]}
OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
print("Wrote",len(payload["items"]),"items")
