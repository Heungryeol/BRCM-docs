"""
수집 출처별 처리 — 대기열 채우기(plan_*)와 문서 1건 수집(fetch_*).

  kb        knowledge.broadcom.com  sitemap → 문서별 수집 (VMware 범위 판정)
  techdocs  techdocs.broadcom.com   sitemap → 지정 경로의 페이지
  github    공식 GitHub 저장소      git clone/pull → md/pdf/csv
  urls      지정 URL                vmware.com/docs/* 백서·설계 가이드 (PDF/HTML)
"""
from __future__ import annotations

import fnmatch
import hashlib
import html
import os
import re
import subprocess
from pathlib import Path

from .extract import extract_file, html_blocks, parse_kb, sections_from_blocks
from .http import Blocked, Fetcher
from .log import log
from .store import Store

LOC = re.compile(r"<loc>\s*([^<]+?)\s*</loc>(?:\s*<lastmod>\s*([^<]+?)\s*</lastmod>)?", re.S)


def sitemap_entries(fetcher: Fetcher, url: str) -> list[tuple[str, str | None]]:
    """sitemap(index 포함) → [(url, lastmod)]"""
    r = fetcher.get(url)
    if not r.ok:
        raise RuntimeError(f"sitemap 응답 {r.status or r.error}: {url}")
    xml = r.text()
    ents = [(html.unescape(u), lm) for u, lm in LOC.findall(xml)]
    if "<sitemapindex" in xml[:2000]:
        out: list[tuple[str, str | None]] = []
        for child, _ in ents:
            if fetcher.stop.is_set():
                break
            out += sitemap_entries(fetcher, child)
        return out
    return ents


# =========================================================================== KB
KB_RE = re.compile(r"/external/article/(\d+)(?:/([^/?#]+?))?(?:\.html)?(?:[?#].*)?$")


def plan_kb(cfg: dict, fetcher: Fetcher, store: Store, slug_skip: bool = True) -> int:
    kc = cfg["kb"]
    vm_tok = {t.lower() for t in kc.get("vmware_slug_tokens", [])}
    non_tok = {t.lower() for t in kc.get("non_vmware_slug_tokens", [])} if slug_skip and kc.get("scope") == "vmware" else set()
    ents = sitemap_entries(fetcher, kc["sitemap"])
    rows, seen = [], set()
    cnt = {"vmware": 0, "other": 0, "slug": 0, "translation": 0}
    for u, lm in ents:
        m = KB_RE.search(u)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        slug = (m.group(2) or "").lower()
        did = f"kb:{m.group(1)}"
        words = set(re.split(r"[-_]+", slug))
        if kc.get("skip_translations", True) and slug and not slug.isascii():
            rows.append((u, "kb", did, lm, 9, "skipped", "translation"))
            cnt["translation"] += 1
        elif words & vm_tok:
            rows.append((u, "kb", did, lm, 0))
            cnt["vmware"] += 1
        elif words & non_tok:
            rows.append((u, "kb", did, lm, 8, "skipped", "slug"))
            cnt["slug"] += 1
        else:
            rows.append((u, "kb", did, lm, 3))
            cnt["other"] += 1
    store.enqueue(rows)
    log(f"KB sitemap {len(seen):,}건 — VMware 단어 {cnt['vmware']:,} / 판정 필요 {cnt['other']:,} / "
        f"비 VMware(slug) {cnt['slug']:,} / 번역판 {cnt['translation']:,}")
    return len(seen)


def fetch_kb(cfg: dict, fetcher: Fetcher, store: Store, row) -> str:
    r = fetcher.get(row["url"])
    if r.status in (404, 410):
        store.put_stub(row["doc_id"], "kb", row["url"], "", "gone", lastmod=row["lastmod"])
        return "gone"
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status or r.error}")
    a = parse_kb(r.text())
    if not a["body_found"]:
        raise RuntimeError("본문 구조 인식 실패 (Issue/Resolution 섹션 없음)")
    kc = cfg["kb"]
    if kc.get("scope") == "vmware":
        hay = a["products"] or (a["title"] + " " + " ".join(t for h, t in a["sections"] if h == "Environment"))
        if not re.search(kc["product_regex"], hay, re.I):
            store.put_stub(row["doc_id"], "kb", row["url"], a["title"], "skipped", a["products"], row["lastmod"], "product")
            return "skipped"
    store.put_doc(row["doc_id"], "kb", row["url"], a["title"], a["sections"], a["products"], a["updated"], row["lastmod"])
    return "done"


# =========================================================================== techdocs
def plan_techdocs(cfg: dict, fetcher: Fetcher, store: Store) -> int:
    tc = cfg["techdocs"]
    ents = sitemap_entries(fetcher, tc["sitemap"])
    pf, ex = tc.get("prefixes") or [], tc.get("exclude") or []
    rows = []
    for u, lm in ents:
        if any(u.startswith(p) for p in pf) and not any(x in u for x in ex):
            path = u.split("/us/en/", 1)[-1]
            depth = path.count("/")
            rows.append((u, "techdocs", "td:" + path, lm, min(depth, 9)))
    if tc.get("max_pages"):
        rows = sorted(rows, key=lambda r: (r[4], r[0]))[: tc["max_pages"]]
    store.enqueue(rows)
    log(f"techdocs sitemap {len(ents):,}건 중 대상 경로 {len(rows):,}건")
    return len(rows)


def fetch_page(source: str):
    def _f(cfg: dict, fetcher: Fetcher, store: Store, row) -> str:
        r = fetcher.get(row["url"])
        if r.status in (404, 410):
            store.put_stub(row["doc_id"], source, row["url"], "", "gone", lastmod=row["lastmod"])
            return "gone"
        if not r.ok:
            raise RuntimeError(f"HTTP {r.status or r.error}")
        if source == "techdocs" and "html" in (r.content_type or "text/html"):
            title, items = html_blocks(r.text())
            secs = sections_from_blocks(items)
        else:
            title, secs = extract_file(row["url"].rsplit("/", 1)[-1], r.data, r.content_type)
        if not secs:
            raise RuntimeError("본문 없음")
        store.put_doc(row["doc_id"], source, row["url"], title or row["url"].rsplit("/", 1)[-1], secs,
                      lastmod=row["lastmod"], meta={"content_type": r.content_type, "final_url": r.url})
        return "done"
    return _f


def plan_urls(cfg: dict, store: Store) -> int:
    rows = [(u, "urls", "url:" + u, None, 1) for u in cfg["urls"].get("list") or []]
    store.enqueue(rows)
    return len(rows)


# =========================================================================== GitHub
def _glob(path: str, pats: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path, p.replace("**/", "")) for p in pats)


def sync_github(cfg: dict, store: Store, data_dir: Path) -> dict:
    gc = cfg["github"]
    stats = {"repos": 0, "new": 0, "same": 0, "removed": 0, "error": 0, "links": 0}
    pats = [re.compile(p) for p in (cfg.get("urls") or {}).get("discover_patterns") or []]
    td_pf = (cfg.get("techdocs") or {}).get("prefixes") or []
    links: set[str] = set()
    for rp in gc.get("repos") or []:
        url, branch = rp["url"].rstrip("/"), rp.get("branch", "main")
        owner_repo = "/".join(url.split("/")[-2:]).removesuffix(".git")
        dest = data_dir / "git" / owner_repo.replace("/", "__")
        env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1", GIT_TERMINAL_PROMPT="0")
        try:
            if (dest / ".git").exists():
                subprocess.run(["git", "-C", str(dest), "fetch", "-q", "--depth", "1", "origin", branch], check=True, env=env)
                subprocess.run(["git", "-C", str(dest), "reset", "-q", "--hard", f"origin/{branch}"], check=True, env=env)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(["git", "clone", "-q", "--depth", "1", "-b", branch, url, str(dest)], check=True, env=env)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log(f"GitHub {owner_repo}: git 실패 — {e}")
            stats["error"] += 1
            continue
        stats["repos"] += 1
        files = sorted(p for p in dest.rglob("*") if p.is_file() and ".git" not in p.parts)
        rel = {str(p.relative_to(dest)): p for p in files}
        chosen = [k for k in rel if _glob(k, rp.get("include") or ["**/*"]) and not _glob(k, rp.get("exclude") or [])]
        if rp.get("prefer_md", True):
            md_stems = {k.rsplit(".", 1)[0].lower() for k in chosen if k.lower().endswith(".md")}
            chosen = [k for k in chosen if not (k.lower().endswith(".pdf") and k.rsplit(".", 1)[0].lower() in md_stems)]
        prefix = f"gh:{owner_repo}:"
        keep = set()
        for k in chosen:
            p = rel[k]
            if p.stat().st_size > gc.get("max_file_mb", 40) * 1048576:
                continue
            data = p.read_bytes()
            if data[:40].startswith(b"version https://git-lfs"):
                continue
            did = prefix + k
            keep.add(did)
            try:
                title, secs = extract_file(k, data)
            except Exception as e:  # noqa: BLE001
                log(f"  {k}: 추출 실패 — {e}")
                stats["error"] += 1
                continue
            if not secs:
                continue
            title = title or Path(k).stem.replace("-", " ")
            changed = store.put_doc(did, "github", f"{url}/blob/{branch}/{k}", title, secs,
                                    meta={"repo": owner_repo, "path": k, "sha256": hashlib.sha256(data).hexdigest()})
            stats["new" if changed else "same"] += 1
            if gc.get("discover_links") and k.lower().endswith((".md", ".markdown")):
                txt = data.decode("utf-8", "replace")
                for rx in pats:
                    links.update(rx.findall(txt))
                links.update(u for u in re.findall(r"https://techdocs\.broadcom\.com/[^\s)\"'<>\]]+", txt)
                             if any(u.startswith(p) for p in td_pf))
        for r in store.db.execute("SELECT id FROM doc WHERE source='github' AND id LIKE ? AND status='ok'", (prefix + "%",)).fetchall():
            if r[0] not in keep:
                store.put_stub(r[0], "github", "", "", "gone", note="저장소에서 삭제됨")
                stats["removed"] += 1
        log(f"GitHub {owner_repo}: 파일 {len(chosen)}개 (갱신 {stats['new']} / 동일 {stats['same']})")
    if links:
        rows = []
        for u in sorted(links):
            u = u.rstrip(".,;")
            if "techdocs.broadcom.com" in u:
                rows.append((u.split("#")[0], "techdocs", "td:" + u.split("/us/en/", 1)[-1].split("#")[0], None, 0))
            else:
                rows.append((u, "urls", "url:" + u, None, 2))
        store.enqueue(rows)
        stats["links"] = len(rows)
        log(f"문서 안의 링크 {len(rows)}건을 대기열에 추가 (vmware.com/docs · techdocs)")
    return stats


# =========================================================================== 로컬 파일
def import_files(store: Store, paths: list[Path], source: str = "local") -> dict:
    st = {"ok": 0, "same": 0, "empty": 0, "error": 0}
    files: list[Path] = []
    for p in paths:
        files += sorted(f for f in p.rglob("*") if f.is_file()) if p.is_dir() else [p]
    for f in files:
        if f.suffix.lower() not in (".pdf", ".html", ".htm", ".md", ".txt", ".csv"):
            continue
        data = f.read_bytes()
        try:
            title, secs = extract_file(f.name, data)
        except Exception as e:  # noqa: BLE001
            log(f"  {f.name}: 추출 실패 — {e}")
            st["error"] += 1
            continue
        if not secs:
            log(f"  {f.name}: 본문 없음 (스캔 PDF 이거나 구조 인식 실패)")
            st["empty"] += 1
            continue
        did = f"{source}:{hashlib.sha256(data).hexdigest()[:16]}"
        changed = store.put_doc(did, source, "", title or f.stem, secs, meta={"file": f.name})
        st["ok" if changed else "same"] += 1
        log(f"  {f.name}: {title or f.stem} — 섹션 {len(secs)}개")
    return st


FETCHERS = {"kb": fetch_kb, "techdocs": fetch_page("techdocs"), "urls": fetch_page("urls")}
__all__ = ["Blocked", "FETCHERS", "plan_kb", "plan_techdocs", "plan_urls", "sync_github", "import_files", "sitemap_entries"]
