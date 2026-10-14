"""
본문 추출 — HTML(KB 문서 · techdocs · 일반 페이지), PDF, Markdown, CSV → 제목 + 섹션 목록.

섹션 = (제목 경로, 본문). 검색·RAG 에 쓰기 좋게 섹션 단위로 자르고, 긴 섹션은 다시 나눕니다.
"""
from __future__ import annotations

import csv
import html
import io
import re
from html.parser import HTMLParser

KB_SECTIONS = ["Issue/Introduction", "Environment", "Cause", "Resolution", "Workaround", "Additional Information"]
KB_SECTION_END = ("Feedback", "Was this article helpful", "Attachments", "thumb_up", "Recommended Content")
WS = re.compile(r"[ \t ​]+")


class _Blocks(HTMLParser):
    """HTML → 줄 목록. 제목(h1~h4)은 ('#', 레벨, 텍스트) 로 표시."""
    SKIP = {"script", "style", "noscript", "svg", "header", "footer", "nav", "form", "button", "template", "iframe"}
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "pre", "section", "article", "dd", "dt",
             "blockquote", "figcaption", "td", "th"}
    HEAD = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[tuple] = []
        self.buf: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False
        self._head: int | None = None
        self._pre = 0

    def _flush(self):
        t = "".join(self.buf)
        self.buf = []
        if self._pre:
            for l in t.splitlines():
                if l.strip():
                    self.items.append(("t", l.rstrip()))
            return
        for l in t.splitlines():
            l = WS.sub(" ", l).strip()
            if l:
                self.items.append(("t", l))

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
            return
        if tag == "title":
            self._in_title = True
        if self.skip:
            return
        if tag in self.HEAD:
            self._flush()
            self._head = self.HEAD[tag]
        elif tag in self.BLOCK:
            self._flush()
            if tag == "li":
                self.buf.append("- ")
            if tag == "pre":
                self._pre += 1
        elif tag in ("td", "th"):
            self.buf.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "title":
            self._in_title = False
        if self.skip:
            return
        if tag in self.HEAD and self._head:
            t = WS.sub(" ", "".join(self.buf)).strip()
            self.buf = []
            if t:
                self.items.append(("#", self._head, t))
            self._head = None
        elif tag in self.BLOCK:
            self._flush()
            if tag == "pre":
                self._pre = max(0, self._pre - 1)

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self.skip:
            self.buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _clean_title(t: str) -> str:
    t = html.unescape(WS.sub(" ", t or "")).strip()
    return re.sub(r"\s*[-|–]\s*(Broadcom|VMware|TechDocs)\b.*$", "", t, flags=re.I).strip()


def html_blocks(page: str, main_only: bool = True) -> tuple[str, list[tuple]]:
    """(문서 제목, 블록 목록). <main>/<article> 이 있으면 그 안만."""
    region = page
    if main_only:
        for pat in (r"<main\b[^>]*>(.*)</main>", r"<article\b[^>]*>(.*)</article>",
                    r'<div[^>]+(?:id|class)="[^"]*(?:main-content|content-body|article-body)[^"]*"[^>]*>(.*)'):
            m = re.search(pat, page, re.S | re.I)
            if m and len(m.group(1)) > 500:
                region = m.group(1)
                break
    p = _Blocks()
    p.feed(region)
    p.close()
    title = ""
    h1 = next((i[2] for i in p.items if i[0] == "#" and i[1] == 1), "")
    if region is not page:
        tp = _Blocks()
        tp.feed(page[:200000])
        tp.close()
        title = tp.title
    else:
        title = p.title
    return _clean_title(h1 or title), p.items


def sections_from_blocks(items: list[tuple], size: int = 1800, min_len: int = 40) -> list[tuple[str, str]]:
    """블록 → [(제목 경로, 본문)] — 제목 경계에서 자르고, 긴 섹션은 size 기준으로 다시 나눔"""
    path: list[str] = []
    out: list[tuple[str, str]] = []
    cur: list[str] = []

    def emit():
        text = "\n".join(cur).strip()
        cur.clear()
        if len(text) < min_len:
            return
        head = " > ".join(path)
        while len(text) > size * 1.3:
            cut = text.rfind("\n", 0, size)
            cut = cut if cut > size // 2 else size
            out.append((head, text[:cut].strip()))
            text = text[cut:].strip()
        out.append((head, text))

    for it in items:
        if it[0] == "#":
            emit()
            lvl = it[1]
            path[:] = path[: max(0, lvl - 1)] + [it[2]]
        else:
            cur.append(it[1])
    emit()
    return out


def lines_to_blocks(lines: list[str]) -> list[tuple]:
    return [("t", l) for l in lines if l.strip()]


# --------------------------------------------------------------------------- KB 문서
def parse_kb(page: str) -> dict:
    """knowledge.broadcom.com 문서 → {title, products, updated, sections[(heading, text)], body_found}"""
    title, items = html_blocks(page, main_only=False)
    lines: list[str] = []
    for it in items:
        lines.append(it[2] if it[0] == "#" else it[1])
    sec: dict[str, list[str]] = {}
    cur = None
    for l in lines:
        key = next((s for s in KB_SECTIONS if l == s or l.rstrip(":") == s), None)
        if key:
            cur = key
            sec.setdefault(cur, [])
            continue
        if cur and l.startswith(KB_SECTION_END):
            cur = None
            continue
        if cur:
            sec[cur].append(l)
    prods: list[str] = []
    if "Products" in lines:
        for l in lines[lines.index("Products") + 1:]:
            if l in KB_SECTIONS or l in ("Show More", "Show Less", "Issue/Introduction") or len(prods) >= 12:
                break
            if len(l) < 80:
                prods.append(l.strip("- "))
    joined = "\n".join(lines)
    m = re.search(r"Updated On:?\s*\n?\s*([0-9]{1,4}[/.-][0-9]{1,2}[/.-][0-9]{1,4})", joined)
    return {
        "title": title,
        "products": "; ".join(dict.fromkeys(p for p in prods if p)),
        "updated": m.group(1) if m else "",
        "sections": [(s, "\n".join(v).strip()) for s, v in ((s, sec.get(s, [])) for s in KB_SECTIONS) if v],
        "body_found": bool(sec),
    }


# --------------------------------------------------------------------------- 파일 형식
def pdf_text(data: bytes) -> tuple[str, list[tuple]]:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError as e:
            raise RuntimeError("PDF 를 읽으려면 pypdf 또는 PyPDF2 가 필요합니다 (apt install python3-pypdf2)") from e
    r = PdfReader(io.BytesIO(data))
    title = ""
    try:
        title = str((r.metadata or {}).get("/Title") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    items: list[tuple] = []
    for n, pg in enumerate(r.pages, 1):
        try:
            txt = pg.extract_text() or ""
        except Exception:  # noqa: BLE001
            txt = ""
        items.append(("#", 2, f"p.{n}"))
        for l in txt.splitlines():
            l = WS.sub(" ", l).strip()
            if l:
                items.append(("t", l))
    return title, items


def md_blocks(text: str) -> tuple[str, list[tuple]]:
    items: list[tuple] = []
    title = ""
    in_code = False
    for l in text.splitlines():
        if l.strip().startswith("```"):
            in_code = not in_code
            continue
        m = None if in_code else re.match(r"^(#{1,4})\s+(.*?)\s*#*\s*$", l)
        if m:
            t = re.sub(r"[*_`]", "", m.group(2)).strip()
            items.append(("#", len(m.group(1)), t))
            if not title and len(m.group(1)) == 1:
                title = t
        elif l.strip():
            items.append(("t", l.rstrip() if in_code else re.sub(r"!\[[^\]]*\]\([^)]*\)", "", l).strip()))
    return title, items


def csv_sections(text: str, title_cols: tuple[str, ...] = ("Description/Title", "Title", "SCG ID", "ID", "Name")) -> list[tuple[str, str]]:
    rows = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    out = []
    for r in rows:
        head = " · ".join(str(r.get(c) or "").strip() for c in title_cols if (r.get(c) or "").strip())[:200]
        body = "\n".join(f"{k}: {str(v).strip()}" for k, v in r.items() if k and v and str(v).strip())
        if body:
            out.append((head, body))
    return out


def extract_file(name: str, data: bytes, content_type: str = "") -> tuple[str, list[tuple[str, str]]]:
    """파일/응답 하나 → (제목, 섹션들)"""
    n = name.lower()
    if n.endswith(".pdf") or data[:5] == b"%PDF-" or content_type == "application/pdf":
        t, items = pdf_text(data)
        return t, sections_from_blocks(items)
    text = data.decode("utf-8", "replace")
    if n.endswith((".md", ".markdown", ".mdown")):
        t, items = md_blocks(text)
        return t, sections_from_blocks(items)
    if n.endswith(".csv"):
        return "", csv_sections(text)
    if n.endswith((".txt", ".ps1", ".psm1", ".yaml", ".yml", ".json")):
        return "", sections_from_blocks(lines_to_blocks(text.splitlines()))
    t, items = html_blocks(text)
    return t, sections_from_blocks(items)
