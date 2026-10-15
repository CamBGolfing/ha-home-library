"""Metadata lookup from public sources: Open Library first, Google Books to fill gaps."""
import logging
import os
import re
import uuid
from pathlib import Path

import httpx

log = logging.getLogger("library.lookup")

GOOGLE_API_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()
USER_AGENT = "HomeLibrary/1.0 (self-hosted home book catalog)"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


# ---------------------------------------------------------------- ISBN helpers

def clean_code(raw: str) -> str:
    return re.sub(r"[^0-9Xx]", "", raw or "").upper()


def isbn10_valid(s: str) -> bool:
    if not re.fullmatch(r"\d{9}[\dX]", s):
        return False
    total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(s))
    return total % 11 == 0


def isbn13_valid(s: str) -> bool:
    if not re.fullmatch(r"\d{13}", s):
        return False
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(s))
    return total % 10 == 0


def isbn10_to_13(s: str) -> str:
    core = "978" + s[:9]
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core))
    return core + str((10 - total % 10) % 10)


def isbn13_to_10(s: str) -> str | None:
    if not s.startswith("978"):
        return None  # 979 prefixes have no ISBN-10 form
    core = s[3:12]
    total = sum((10 - i) * int(c) for i, c in enumerate(core))
    check = (11 - total % 11) % 11
    return core + ("X" if check == 10 else str(check))


def normalize(raw: str) -> dict:
    """Turn a scanned or typed code into {isbn13, isbn10, kind}.

    kind is 'isbn' for a real ISBN, 'ean' for some other product barcode
    (e.g. a UPC on a mass-market paperback), or 'invalid'.
    """
    code = clean_code(raw)
    if len(code) == 10 and isbn10_valid(code):
        return {"isbn13": isbn10_to_13(code), "isbn10": code, "kind": "isbn"}
    if len(code) == 13 and isbn13_valid(code):
        if code.startswith(("978", "979")):
            return {"isbn13": code, "isbn10": isbn13_to_10(code), "kind": "isbn"}
        return {"isbn13": code, "isbn10": None, "kind": "ean"}
    if len(code) == 12 and code.isdigit():  # UPC-A
        return {"isbn13": "0" + code, "isbn10": None, "kind": "ean"}
    return {"isbn13": None, "isbn10": None, "kind": "invalid"}


# ---------------------------------------------------------------- HTTP

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    )


async def _get_json(client: httpx.AsyncClient, url: str, params=None):
    try:
        r = await client.get(url, params=params)
        if r.status_code == 200:
            return r.json()
        log.info("lookup %s -> HTTP %s", url, r.status_code)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("lookup %s failed: %s", url, e)
    return None


# ---------------------------------------------------------------- Open Library

def _parse_openlibrary(d: dict) -> dict:
    ids = d.get("identifiers") or {}
    cover = d.get("cover") or {}
    return {
        "title": d.get("title") or "",
        "subtitle": d.get("subtitle") or "",
        "authors": ", ".join(a.get("name", "") for a in d.get("authors") or [] if a.get("name")),
        "publisher": ", ".join(p.get("name", "") for p in d.get("publishers") or [] if p.get("name")),
        "published": d.get("publish_date") or "",
        "pages": d.get("number_of_pages"),
        "subjects": ", ".join(s.get("name", "") for s in (d.get("subjects") or [])[:8] if s.get("name")),
        "isbn10": (ids.get("isbn_10") or [None])[0],
        "isbn13": (ids.get("isbn_13") or [None])[0],
        "cover_urls": [u for u in (cover.get("large"), cover.get("medium")) if u],
        "url": d.get("url") or "",
    }


async def openlibrary_isbn(client, isbn: str) -> dict | None:
    data = await _get_json(
        client,
        "https://openlibrary.org/api/books",
        {"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
    )
    if not data:
        return None
    entry = data.get(f"ISBN:{isbn}")
    return _parse_openlibrary(entry) if entry else None


# ---------------------------------------------------------------- Google Books

def _gb_image(links: dict) -> list[str]:
    out = []
    for key in ("extraLarge", "large", "medium", "thumbnail", "smallThumbnail"):
        u = links.get(key)
        if u:
            u = u.replace("http://", "https://").replace("&edge=curl", "")
            out.append(u)
    return out


def _parse_google(item: dict) -> dict:
    v = item.get("volumeInfo") or {}
    ids = {i.get("type"): i.get("identifier") for i in v.get("industryIdentifiers") or []}
    return {
        "title": v.get("title") or "",
        "subtitle": v.get("subtitle") or "",
        "authors": ", ".join(v.get("authors") or []),
        "publisher": v.get("publisher") or "",
        "published": v.get("publishedDate") or "",
        "pages": v.get("pageCount") or None,
        "language": v.get("language") or "",
        "description": re.sub(r"<[^>]+>", "", v.get("description") or "").strip(),
        "subjects": ", ".join(v.get("categories") or []),
        "isbn10": ids.get("ISBN_10"),
        "isbn13": ids.get("ISBN_13"),
        "cover_urls": _gb_image(v.get("imageLinks") or {}),
        "url": v.get("infoLink") or "",
    }


async def google_query(client, q: str, limit: int = 1) -> list[dict]:
    params = {"q": q, "maxResults": limit, "printType": "books"}
    if GOOGLE_API_KEY:
        params["key"] = GOOGLE_API_KEY
    data = await _get_json(client, "https://www.googleapis.com/books/v1/volumes", params)
    return [_parse_google(i) for i in (data or {}).get("items") or []]


# ---------------------------------------------------------------- merge

MERGE_FIELDS = ("title", "subtitle", "authors", "publisher", "published", "pages",
                "language", "description", "subjects", "isbn10", "isbn13")


def _merge(primary: dict | None, secondary: dict | None) -> dict:
    out: dict = {}
    sources = []
    for src_name, src in (("Open Library", primary), ("Google Books", secondary)):
        if not src:
            continue
        sources.append(src_name)
        for f in MERGE_FIELDS:
            if not out.get(f) and src.get(f):
                out[f] = src[f]
    # Google descriptions are usually better; Open Library 'data' has none.
    if secondary and secondary.get("description"):
        out["description"] = secondary["description"]
    out["cover_urls"] = (primary or {}).get("cover_urls", []) + (secondary or {}).get("cover_urls", [])
    out["source"] = " + ".join(sources)
    return out


async def lookup_isbn(raw: str) -> dict:
    """Look up a scanned/typed code. Always returns a dict with 'found'."""
    norm = normalize(raw)
    if norm["kind"] == "invalid":
        return {"found": False, "error": "That doesn't look like a valid ISBN or barcode.", **norm}

    isbn = norm["isbn13"]
    async with _client() as client:
        ol = await openlibrary_isbn(client, isbn) if norm["kind"] == "isbn" else None
        gb_list = await google_query(client, f"isbn:{isbn}")
        if not gb_list and norm["isbn10"]:
            gb_list = await google_query(client, f"isbn:{norm['isbn10']}")
        gb = gb_list[0] if gb_list else None

    if not ol and not gb:
        return {"found": False, "error": "No public record found for this barcode. You can add it by hand.", **norm}

    merged = _merge(ol, gb)
    # Fall back to the Open Library covers-by-ISBN endpoint as a last resort.
    if norm["kind"] == "isbn":
        merged["cover_urls"].append(f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg?default=false")
    # Trust the scanned code over whatever the sources say.
    if norm["kind"] == "isbn":
        merged["isbn13"] = isbn
        merged["isbn10"] = norm["isbn10"] or merged.get("isbn10")
    else:
        merged["isbn13"] = merged.get("isbn13") or isbn
    merged["found"] = True
    merged["kind"] = norm["kind"]
    return merged


async def search_title(q: str) -> list[dict]:
    """Free-text search for books without a barcode."""
    results: list[dict] = []
    async with _client() as client:
        data = await _get_json(
            client,
            "https://openlibrary.org/search.json",
            {"q": q, "limit": 12,
             "fields": "key,title,subtitle,author_name,first_publish_year,isbn,cover_i,publisher,number_of_pages_median"},
        )
        for d in (data or {}).get("docs") or []:
            isbns = d.get("isbn") or []
            isbn13 = next((i for i in isbns if len(i) == 13), None)
            isbn10 = next((i for i in isbns if len(i) == 10), None)
            cover = f"https://covers.openlibrary.org/b/id/{d['cover_i']}-L.jpg" if d.get("cover_i") else None
            results.append({
                "title": d.get("title") or "",
                "subtitle": d.get("subtitle") or "",
                "authors": ", ".join((d.get("author_name") or [])[:3]),
                "published": str(d.get("first_publish_year") or ""),
                "publisher": (d.get("publisher") or [""])[0],
                "pages": d.get("number_of_pages_median"),
                "isbn13": isbn13,
                "isbn10": isbn10,
                "cover_urls": [cover] if cover else [],
                "source": "Open Library",
            })
        if len(results) < 4:
            for g in await google_query(client, q, limit=8):
                g["source"] = "Google Books"
                results.append(g)
    return results


# ---------------------------------------------------------------- covers

async def download_cover(urls: list[str], covers_dir: Path) -> str:
    """Try each URL in order; save the first real image. Returns filename or ''."""
    async with _client() as client:
        for url in urls:
            if not url or not url.startswith(("http://", "https://")):
                continue
            try:
                r = await client.get(url)
            except httpx.HTTPError as e:
                log.info("cover %s failed: %s", url, e)
                continue
            ctype = r.headers.get("content-type", "")
            # Tiny responses are placeholder pixels / "image not available" stubs.
            if r.status_code != 200 or not ctype.startswith("image/") or len(r.content) < 2000:
                continue
            ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(ctype.split(";")[0], ".jpg")
            name = f"{uuid.uuid4().hex}{ext}"
            (covers_dir / name).write_bytes(r.content)
            return name
    return ""
