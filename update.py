#!/usr/bin/env python3
from __future__ import annotations
import argparse
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

ROOT=Path(__file__).resolve().parent
CFG=json.loads((ROOT/"sources.json").read_text(encoding="utf-8"))
OUT=ROOT/"news.json"
UA="Mozilla/5.0 Fotbollsbriefen/2.1"
NOW=dt.datetime.now(dt.timezone.utc)
CUTOFF=NOW-dt.timedelta(days=CFG.get("max_age_days",7))
RESULT_WORDS=re.compile(r"\b(score|result|highlights|standings|table|match report|beat|defeat|draw|full-time|final score|vs)\b",re.I)
OFFICIAL={x["name"] for x in CFG["feeds"] if x.get("priority",0)>=100}

def fetch(url:str,timeout:int=20)->bytes:
    req=urllib.request.Request(url,headers={"User-Agent":UA})
    with urllib.request.urlopen(req,timeout=timeout,context=ssl.create_default_context()) as response:
        return response.read()

def node_text(node:ET.Element,names:list[str])->str:
    for name in names:
        child=node.find(name)
        if child is not None and child.text:return child.text.strip()
    return ""

def clean(value:str)->str:
    value=html.unescape(re.sub(r"<[^>]+>"," ",value or ""))
    return re.sub(r"\s+"," ",value).strip()

def parse_date(value:str)->dt.datetime|None:
    try:
        parsed=email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        try:return dt.datetime.fromisoformat(value.replace("Z","+00:00")).astimezone(dt.timezone.utc)
        except Exception:return None

def google_url(query:str)->str:
    query=f'{query} when:{CFG.get("max_age_days",7)}d'
    return "https://news.google.com/rss/search?"+urllib.parse.urlencode({"q":query,"hl":"en-GB","gl":"GB","ceid":"GB:en"})

def categories(title:str)->list[str]:
    t=title.lower();out=[]
    pairs=[
      ("Premier League",r"manchester|liverpool|arsenal|chelsea|tottenham|newcastle|aston villa|premier league"),
      ("La Liga",r"barcelona|real madrid|atl[eé]tico|la liga"),
      ("Serie A",r"juventus|milan|inter|napoli|roma|lazio|atalanta|serie a"),
      ("Bundesliga",r"bayern|dortmund|leverkusen|bundesliga"),
      ("Ligue 1",r"psg|paris saint|marseille|monaco|ligue 1"),
      ("Transfer",r"transfer|sign|deal|bid|joins|move|contract|renew"),
      ("Tränare",r"manager|coach|head coach|trainer")]
    for cat,pattern in pairs:
        if re.search(pattern,t):out.append(cat)
    return out or ["Fotboll"]

def source_status(source:str)->str:
    if source in OFFICIAL:return "Officiellt"
    if source in {"Reuters","BBC Sport","Simon Stone / BBC Sport"}:return "Bekräftat"
    if source in {"David Ornstein / The Athletic","Fabrizio Romano","Gianluca Di Marzio"}:return "Mycket trovärdigt"
    return "Rapporterat"

def normalized(value:str)->str:
    value=re.sub(r"[^a-z0-9 ]"," ",value.lower())
    return re.sub(r"\s+"," ",value).strip()

def short_summary(description:str,title:str)->str:
    description=clean(description)
    if not description:return f"{title}. Läs mer hos källan."
    sentences=re.split(r"(?<=[.!?])\s+",description)
    return " ".join(sentences[:4]).strip()[:850]

def read_existing()->list[dict]:
    try:
        payload=json.loads(OUT.read_text(encoding="utf-8"))
        return [x for x in payload.get("items",[]) if parse_date(x.get("published_at","")) and parse_date(x["published_at"])>=CUTOFF]
    except Exception:return []

def parse_feed(xml_bytes:bytes,source:dict)->list[dict]:
    root=ET.fromstring(xml_bytes);items=[]
    for node in root.findall(".//item"):
        title=clean(node_text(node,["title"]))
        link=clean(node_text(node,["link"]))
        published=parse_date(node_text(node,["pubDate","published","updated"]))
        description=node_text(node,["description","summary"])
        if not title or not link or not published or published<CUTOFF or RESULT_WORDS.search(title):continue
        if " - " in title and source["type"]=="google":title=title.rsplit(" - ",1)[0].strip()
        cats=categories(title)
        items.append({
          "published_at":published.isoformat(),"updated_at":published.isoformat(),
          "title":title,"summary":short_summary(description,title),"category":cats,
          "status":source_status(source["name"]),"entity":"","entity_type":"coaches" if "Tränare" in cats else "players",
          "image":"","source_priority":source.get("priority",50),
          "sources":[{"name":source["name"],"url":link}]})
    return items

def merge(items:list[dict])->list[dict]:
    items.sort(key=lambda x:(x.get("source_priority",50),x["published_at"]),reverse=True)
    groups=[]
    for item in items:
        match=None
        for group in groups:
            if SequenceMatcher(None,normalized(item["title"]),normalized(group[0]["title"])).ratio()>=0.72:
                match=group;break
        if match is not None:
            match.append(item)
        else:
            groups.append([item])
    merged=[]
    for group in groups:
        group.sort(key=lambda x:x.get("source_priority",50),reverse=True)
        lead=dict(group[0]);names=set();sources=[]
        for item in group:
            for source in item.get("sources",[]):
                if source["name"] not in names:names.add(source["name"]);sources.append(source)
        lead["sources"]=sources
        if any(x.get("status")=="Officiellt" for x in group):lead["status"]="Officiellt"
        elif len(sources)>1:lead["status"]="Bekräftat av flera källor"
        lead["id"]=re.sub(r"[^a-z0-9]+","-",normalized(lead["title"])).strip("-")[:90]
        lead.pop("source_priority",None)
        merged.append(lead)
    return merged

def main(fixture:Path|None=None)->int:
    collected=[]
    if fixture:
        source={"name":"Testkälla","type":"rss","priority":80}
        collected.extend(parse_feed(fixture.read_bytes(),source))
    else:
        for source in CFG["feeds"]:
            url=source["url"] if source["type"]=="rss" else google_url(source["query"])
            try:collected.extend(parse_feed(fetch(url),source))
            except Exception as exc:print("WARN",source["name"],exc)

    existing=read_existing()
    if not collected:
        print("No fresh items fetched; keeping existing news.json unchanged")
        return 0

    combined=collected+existing
    merged=merge(combined)
    payload={"updated_at":NOW.isoformat(),"items":merged[:CFG.get("max_items",120)]}
    if not payload["items"]:
        print("Refusing to overwrite news.json with zero items")
        return 1
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Wrote",len(payload["items"]),"items")
    return 0

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--fixture",type=Path)
    args=parser.parse_args()
    raise SystemExit(main(args.fixture))
