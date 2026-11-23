"""Gathers today's market picture: index/FX/commodity levels (yfinance) + headlines (free RSS feeds).
Every source is wrapped so one failure never kills the digest - the bot just says what it could not see."""
import re
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
from html import unescape
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

# Yahoo Finance symbols. (name -> symbol)
TICKERS = {
    "Nifty 50": "^NSEI",
    "Sensex": "^BSESN",
    "Nifty Bank": "^NSEBANK",
    "Nifty Midcap 150": "NIFTYMIDCAP150.NS",
    "Nifty Smallcap 250": "NIFTYSMLCAP250.NS",
    "Nifty IT": "^CNXIT",
    "Nifty Pharma": "^CNXPHARMA",
    "Nifty Auto": "^CNXAUTO",
    "Nifty Metal": "^CNXMETAL",
    "Nifty Realty": "^CNXREALTY",
    "Nifty FMCG": "^CNXFMCG",
    "Nifty Energy": "^CNXENERGY",
    "Nifty PSU Bank": "^CNXPSUBANK",
    "Nifty Infrastructure": "^CNXINFRA",
    "India VIX": "^INDIAVIX",
    "USD/INR": "INR=X",
    "Brent crude": "BZ=F",
    "Gold (USD)": "GC=F",
    "US 10Y yield": "^TNX",
    "Dollar index": "DX-Y.NYB",
    "S&P 500": "^GSPC",
    "Nasdaq": "^IXIC",
    "Nikkei 225": "^N225",
    "Hang Seng": "^HSI",
}

# Free RSS feeds. If one changes or dies, it is skipped; add your own in FEEDS.
FEEDS = {
    "Economic Times Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Moneycontrol": "https://www.moneycontrol.com/rss/marketreports.xml",
    "Livemint Markets": "https://www.livemint.com/rss/markets",
    "Business Standard Markets": "https://www.business-standard.com/rss/markets-106.rss",
    "Google News: Indian market": "https://news.google.com/rss/search?q=Sensex+Nifty+FII&hl=en-IN&gl=IN&ceid=IN:en",
    "Google News: RBI/economy": "https://news.google.com/rss/search?q=RBI+rupee+India+economy&hl=en-IN&gl=IN&ceid=IN:en",
}


def fetch_quotes() -> dict:
    """Fetch daily quotes in one batch. Values are explicitly labelled with source and timestamps."""
    try:
        import yfinance as yf
    except ImportError:
        return {}
    out = {}
    try:
        raw = yf.download(list(TICKERS.values()), period="10d", interval="1d", group_by="ticker",
                          auto_adjust=False, progress=False, threads=True)
        retrieved = datetime.now(timezone.utc).isoformat()
        for name, sym in TICKERS.items():
            try:
                if hasattr(raw.columns, "levels") and sym in raw.columns.get_level_values(0):
                    frame = raw[sym]
                    close_series = frame["Close"]
                elif hasattr(raw.columns, "levels") and sym in raw.columns.get_level_values(1):
                    close_series = raw.xs("Close", axis=1, level=0)[sym]
                else:
                    close_series = raw["Close"]
                closes = close_series.dropna()
                if len(closes) < 2:
                    continue
                last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
                if not prev:
                    continue
                out[name] = {"last": round(last, 2), "prev": round(prev, 2),
                             "chg_pct": round((last - prev) / prev * 100, 2),
                             "quote_time": str(closes.index[-1]), "retrieved_at": retrieved,
                             "source": "Yahoo Finance (unofficial)", "period": "daily bar"}
            except Exception:
                continue
    except Exception:
        return {}
    return out


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _read_feed(source: str, url: str) -> list:
    try:
        response = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0 (munim-bot)"})
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except (requests.RequestException, ET.ParseError):
        logger.warning("RSS unavailable: %s", source)
        return []
    retrieved = datetime.now(timezone.utc).isoformat()
    articles = []
    for item in root.iter("item"):
        title = _clean(item.findtext("title"))
        if title:
            publisher = _clean(item.findtext("source")) or source
            articles.append({
                "source": publisher[:100],
                "title": title.removesuffix(f" - {publisher}")[:200],
                "summary": _clean(item.findtext("description"))[:160],
                "published_at": item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or "",
                "url": (item.findtext("link") or "").strip(),
                "retrieved_at": retrieved,
            })
    return articles


def _publication_time(article: dict) -> datetime:
    value = article["published_at"]
    try:
        try:
            published = parsedate_to_datetime(value)
        except ValueError:
            published = datetime.fromisoformat(value)
        if published.tzinfo is not None:
            return published
    except (ValueError, OverflowError):
        pass
    return datetime.min.replace(tzinfo=timezone.utc)


def fetch_headlines(per_feed: int = 8) -> list:
    """Rank recent items before cutting off feeds, then deduplicate across publishers."""
    with ThreadPoolExecutor(max_workers=len(FEEDS)) as executor:
        feeds = list(executor.map(_read_feed, FEEDS, FEEDS.values()))
    seen, candidates = set(), []
    for articles in feeds:
        candidates.extend(sorted(articles, key=_publication_time, reverse=True)[:per_feed])
    out = []
    for article in sorted(candidates, key=_publication_time, reverse=True):
        key = re.sub(r"\W+", "", article["title"].lower())[:70]
        if key not in seen:
            seen.add(key)
            out.append(article)
    return out


def source_links(headlines: list, source_ids: list) -> str:
    """Resolve model-selected references to URLs from the original feed items."""
    links = {}
    for source_id in source_ids:
        if type(source_id) is not int or not 1 <= source_id <= len(headlines):
            continue
        article = headlines[source_id - 1]
        url = article.get("url", "")
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.hostname and len(url) <= 600:
            links.setdefault(url, f"[{source_id}] {article['source']}\n{url}")
        if len(links) == 3:
            break
    return "\n\nSources\n" + "\n".join(links.values()) if links else ""


def format_snapshot(quotes: dict, headlines: list, max_headlines: int = 18) -> str:
    lines = ["MARKET LEVELS (daily bars vs previous close; today's bar may be provisional):"]
    if quotes:
        for name, q in quotes.items():
            lines.append(f"- {name}: {q['last']:,} ({q['chg_pct']:+.2f}%); session={q.get('quote_time', 'unknown')[:10]}")
        quote = next(iter(quotes.values()))
        lines.append(f"Source: {quote.get('source', 'unknown')}; retrieved={quote.get('retrieved_at', 'unknown')}")
    else:
        lines.append("- (price data unavailable this run - say so, do not invent levels)")
    sector_names = ["Nifty IT", "Nifty Pharma", "Nifty Auto", "Nifty Metal", "Nifty Realty",
                    "Nifty FMCG", "Nifty Energy", "Nifty PSU Bank", "Nifty Infrastructure"]
    available_sectors = [(name, quotes[name]["chg_pct"]) for name in sector_names if name in quotes]
    if available_sectors:
        ranked = sorted(available_sectors, key=lambda item: item[1], reverse=True)
        lines.append("SECTOR RELATIVE PERFORMANCE (daily bars; available sectors only):")
        lines.extend(f"- {name}: {change:+.2f}%" for name, change in ranked)
    else:
        lines.append("SECTOR RELATIVE PERFORMANCE: unavailable this run.")
    lines.append("MARKET BREADTH: unavailable from the configured data sources; do not infer advance/decline breadth from index returns.")
    lines.append("")
    lines.append("HEADLINES:")
    if headlines:
        for index, h in enumerate(headlines[:max_headlines], 1):
            summary = h.get("summary", "")
            extra = f" - {summary}" if summary and summary[:40] != h["title"][:40] else ""
            lines.append(f"- [{index}] [{h['source']}; published={h.get('published_at') or 'unknown'}] {h['title']}{extra}")
    else:
        lines.append("- (no headlines fetched this run - say so, do not invent news)")
    return "\n".join(lines)
