"""
종단 시험 — 로컬 가짜 사이트(KB·techdocs·vmware.com/docs)와 로컬 git 저장소로 전체 흐름을 확인.
실제 Broadcom 사이트에는 접속하지 않습니다.

  python3 tests/test_e2e.py
"""
from __future__ import annotations

import http.server
import json
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from brcm_docs.cli import main  # noqa: E402
from brcm_docs.store import Store  # noqa: E402

KB_PAGE = """<html><head><title>{title} | Broadcom</title></head><body><header>menu</header>
<h1>{title}</h1><div>Article ID: {id}</div><div>Updated On: 2026-09-01</div>
<div>Products</div><div>{products}</div><div>Show More</div>
<h3>Issue/Introduction</h3><p>{issue}</p>
<h3>Environment</h3><p>{env}</p>
<h3>Resolution</h3><ol><li>Run <code>vim-cmd hostsvc/enable_ssh</code></li><li>Verify in the vSphere Client.</li></ol>
<h3>Feedback</h3><p>Was this article helpful?</p><footer>footer</footer></body></html>"""

TD_PAGE = """<html><head><title>Configure NIC Teaming | VMware Cloud Foundation</title></head><body><nav>nav</nav>
<main><h1>Configure NIC Teaming, Failover, and Load Balancing</h1>
<p>NIC teaming lets you increase the network capacity of a virtual switch by including two or more physical NICs in a team.
To determine how the traffic is rerouted in case of adapter failure, you include physical NICs in a failover order.</p>
<h2>Load Balancing Algorithms</h2><p>Route based on physical NIC load is available only on a vSphere Distributed Switch.
The distributed switch calculates uplinks by taking the port and checking the load every 30 seconds.</p>
<pre>esxcli network vswitch standard policy failover get -v vSwitch0</pre></main></body></html>"""


def make_server(routes: dict, block: bool = False):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.server.hits.append(self.path)
            if block:
                self.send_response(403)
                self.end_headers()
                return
            body = routes.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            ctype, data = body
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(data if isinstance(data, bytes) else data.encode())

        def log_message(self, *a):
            pass

    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    s.hits = []
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def run() -> list[str]:
    errors: list[str] = []

    def check(c: bool, msg: str):
        print(("  ok   " if c else "  FAIL ") + msg)
        if not c:
            errors.append(msg)

    td = Path(tempfile.mkdtemp(prefix="brcm-test-"))
    routes: dict = {}
    srv = make_server(routes)
    blk = make_server({}, block=True)
    base = f"http://127.0.0.1:{srv.server_port}"
    xml = lambda locs: "<?xml version='1.0'?><urlset>" + "".join(  # noqa: E731
        f"<url><loc>{base}{u}</loc><lastmod>{lm}</lastmod></url>" for u, lm in locs) + "</urlset>"
    routes.update({
        "/robots.txt": ("text/plain", "User-agent: *\nAllow: /\n"),
        "/sitemap.xml": ("application/xml", f"<sitemapindex><sitemap><loc>{base}/sitemap_1.xml</loc></sitemap></sitemapindex>"),
        "/sitemap_1.xml": ("application/xml", xml([
            ("/external/article/312060/enable-ssh-on-esxi-hosts-in-vcf.html", "2026-09-01"),
            ("/external/article/3881/ama534e-during-unpack-of-tersed-file.html", "2025-01-01"),
            ("/external/article/5000/symantec-endpoint-agent-crash.html", "2025-01-01"),
            ("/external/article/436243/%E3%83%9B%E3%82%B9%E3%83%88-esxi.html".replace("%E3%83%9B%E3%82%B9%E3%83%88", "ホスト"), "2025-01-01"),
            ("/external/article/999/removed-esxi-article.html", "2025-01-01"),
        ])),
        "/external/article/312060/enable-ssh-on-esxi-hosts-in-vcf.html": ("text/html", KB_PAGE.format(
            id=312060, title="Enable SSH on ESXi Hosts in VCF", products="VMware Cloud Foundation",
            issue="SSH must be enabled on ESXi hosts for troubleshooting.", env="VMware Cloud Foundation 9.x")),
        "/external/article/3881/ama534e-during-unpack-of-tersed-file.html": ("text/html", KB_PAGE.format(
            id=3881, title="AMA534E during unpack of tersed file", products="CA 1 Tape Management",
            issue="Mainframe unpack error.", env="z/OS")),
        "/td-sitemap.xml": ("application/xml", xml([("/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/networking/teaming.html", "2026-08-01"),
                                                     ("/us/en/other-product/x.html", "2026-08-01")])),
        "/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/networking/teaming.html": ("text/html", TD_PAGE),
        "/docs/perf-best-practices": ("text/html", "<html><head><title>Performance Best Practices for vSphere</title></head><body>"
                                      "<article><h1>Performance Best Practices</h1>" + "<p>Use VMXNET3 virtual NICs for best network performance on ESXi.</p>" * 12
                                      + "</article></body></html>"),
    })

    # 로컬 git 저장소 (베스트 프랙티스)
    repo = td / "src" / "vmware" / "bp-guides"
    (repo / "design").mkdir(parents=True)
    (repo / "design" / "hardening.md").write_text(
        "# ESXi Hardening\n\nDisable the ESXi Shell and SSH when not in use. See "
        f"{base}/docs/linked-guide and https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/security.html\n\n"
        "## Lockdown Mode\n\nEnable normal lockdown mode on all hosts managed by vCenter to restrict direct access.\n", encoding="utf-8")
    (repo / "design" / "hardening.pdf").write_bytes(b"%PDF-1.4 dummy")  # 같은 이름 md 가 있으므로 무시되어야 함
    (repo / "controls.csv").write_text("SCG ID,Description/Title,Baseline Suggested Value\nesxi-9.ssh,Disable SSH,false\n", encoding="utf-8")
    subprocess.run("git init -q -b main && git add -A && git -c user.email=t@t -c user.name=t commit -qm init",
                   shell=True, cwd=repo, check=True)

    cfg = {
        "user_agent": "brcm-docs-test", "delay": 0, "data_dir": str(td / "data"),
        "kb": {"sitemap": f"{base}/sitemap.xml"},
        "techdocs": {"sitemap": f"{base}/td-sitemap.xml", "prefixes": [f"{base}/us/en/vmware-cis/vcf/"]},
        "github": {"repos": [{"url": str(repo), "branch": "main", "include": ["**/*.md", "**/*.pdf", "**/*.csv"]}]},
        "urls": {"list": [f"{base}/docs/perf-best-practices"],
                 "discover_patterns": [f"{base}/docs/[a-z0-9-]+".replace(".", r"\.")]},
    }
    cp = td / "test.yaml"
    cp.write_text(json.dumps(cfg), encoding="utf-8")   # JSON 은 YAML 의 부분집합

    check(main(["--config", str(cp), "crawl"]) == 0, "crawl 전체 실행")
    st = Store(td / "data" / "corpus.sqlite")
    q = {r["url"].rsplit("/", 2)[-2] if "/article/" in r["url"] else r["url"]: (r["state"], r["note"])
         for r in st.db.execute("SELECT * FROM queue WHERE source='kb'")}
    check(q.get("312060", ("",))[0] == "done", f"VMware KB 저장 {q.get('312060')}")
    check(q.get("3881") == ("skipped", None), f"비 VMware 제품 KB 는 범위 밖 {q.get('3881')}")
    check(q.get("5000") == ("skipped", "slug"), f"slug 로 비 VMware 판정(받지 않음) {q.get('5000')}")
    check(q.get("436243") == ("skipped", "translation"), f"번역판 제외 {q.get('436243')}")
    check(q.get("999", ("",))[0] == "gone", f"404 → 삭제됨 {q.get('999')}")
    check(not any("5000" in h or "436243" in h for h in srv.hits), "제외 대상은 요청하지 않음")
    d = st.db.execute("SELECT * FROM doc WHERE id='kb:312060'").fetchone()
    check(d and d["title"] == "Enable SSH on ESXi Hosts in VCF" and d["products"] == "VMware Cloud Foundation", "KB 제목·제품")
    secs = [r["heading"] for r in st.db.execute("SELECT heading FROM section WHERE doc_id='kb:312060' ORDER BY seq")]
    check(secs == ["Issue/Introduction", "Environment", "Resolution"], f"KB 섹션 {secs}")
    res = st.db.execute("SELECT text FROM section WHERE doc_id='kb:312060' AND heading='Resolution'").fetchone()[0]
    check("vim-cmd hostsvc/enable_ssh" in res and "Was this article" not in res, "KB 해결 방법 본문")
    td_doc = st.db.execute("SELECT * FROM doc WHERE source='techdocs'").fetchall()
    check(len(td_doc) == 1 and td_doc[0]["title"].startswith("Configure NIC Teaming"), "techdocs 경로 필터·제목")
    th = [r[0] for r in st.db.execute("SELECT heading FROM section WHERE doc_id=? ORDER BY seq", (td_doc[0]["id"],))] if td_doc else []
    check(any("Load Balancing Algorithms" in h for h in th), f"techdocs 섹션 제목 경로 {th}")
    gh = {r["id"].split(":", 2)[-1] for r in st.db.execute("SELECT id FROM doc WHERE source='github' AND status='ok'")}
    check(gh == {"design/hardening.md", "controls.csv"}, f"GitHub md 우선·csv {gh}")
    links = {r["url"]: r["state"] for r in st.db.execute("SELECT url, state FROM queue WHERE source IN ('urls','techdocs')")}
    check(f"{base}/docs/linked-guide" in links, "문서 안 링크 발견 → 대기열")
    check(links.get(f"{base}/docs/linked-guide") == "gone", "발견 링크 수집(404 → 삭제됨)")
    check(st.db.execute("SELECT COUNT(*) FROM doc WHERE source='urls' AND status='ok'").fetchone()[0] == 1, "URL 문서 저장")
    hits = st.search("lockdown mode", 3)
    check(bool(hits) and hits[0]["source"] == "github", "전문검색 (github)")
    hits = st.search("enable ssh", 3, "kb")
    check(bool(hits) and hits[0]["id"] == "kb:312060", "전문검색 (kb)")

    # 두 번째 실행: 바뀐 것 없음 → KB 재요청 없음
    n_hits = len([h for h in srv.hits if "/external/article/" in h])
    check(main(["--config", str(cp), "crawl"]) == 0, "재실행")
    check(len([h for h in srv.hits if "/external/article/" in h]) == n_hits, "변경 없는 문서는 다시 받지 않음")

    # lastmod 가 바뀌면 다시 받음
    routes["/sitemap_1.xml"] = (routes["/sitemap_1.xml"][0], routes["/sitemap_1.xml"][1].replace("2026-09-01", "2026-10-01"))
    main(["--config", str(cp), "crawl", "-s", "kb"])
    check(len([h for h in srv.hits if "/external/article/312060" in h]) == 2, "lastmod 변경 → 다시 수집")

    # 자동 수집 차단 사이트: 우회하지 않고 blocked
    cfg2 = dict(cfg, data_dir=str(td / "data2"), kb={"enabled": False}, github={"enabled": False}, urls={"enabled": False},
                techdocs={"sitemap": f"http://127.0.0.1:{blk.server_port}/td-sitemap.xml", "prefixes": ["x"]})
    cp2 = td / "test2.yaml"
    cp2.write_text(json.dumps(cfg2), encoding="utf-8")
    check(main(["--config", str(cp2), "crawl"]) == 0, "차단 사이트에서도 정상 종료")
    check(blk.hits == ["/robots.txt"], f"차단 사이트에는 robots.txt 외 요청 없음 {blk.hits}")

    # 로컬 문서 import
    loc = td / "manual"
    loc.mkdir()
    (loc / "saved.html").write_text(TD_PAGE, encoding="utf-8")
    check(main(["--config", str(cp), "import", str(loc), "--as-source", "techdocs-local"]) == 0, "import")
    check(st.db.execute("SELECT COUNT(*) FROM doc WHERE source='techdocs-local'").fetchone()[0] == 1, "import 저장")

    # export 3종
    out = td / "dist"
    for fmt in ("sqlite", "md", "jsonl"):
        check(main(["--config", str(cp), "export", "-f", fmt, "-o", str(out)]) == 0, f"export {fmt}")
    tg = sorted(out.glob("brcm-docs-sqlite-*.tar.gz"))[0]
    with tarfile.open(tg) as tf:
        names = tf.getnames()
        man = json.load(tf.extractfile([n for n in names if n.endswith("manifest.json")][0]))
        dbm = tf.extractfile([n for n in names if n.endswith("corpus.sqlite")][0]).read()
    check(man["by_source"].get("kb") == 1 and "techdocs-local" in man["by_source"], f"export manifest {man['by_source']}")
    import sqlite3
    p = td / "x.sqlite"
    p.write_bytes(dbm)
    c = sqlite3.connect(p)
    tabs = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check("queue" not in tabs and c.execute("SELECT COUNT(*) FROM doc WHERE status!='ok'").fetchone()[0] == 0,
          "export DB: 대기열·stub 제외")
    check(c.execute("SELECT COUNT(*) FROM fts WHERE fts MATCH 'teaming'").fetchone()[0] >= 1, "export DB 전문검색")
    with tarfile.open(sorted(out.glob("brcm-docs-md-*.tar.gz"))[0]) as tf:
        md = [n for n in tf.getnames() if n.endswith("kb_312060.md")]
        txt = tf.extractfile(md[0]).read().decode() if md else ""
    check(txt.startswith("# Enable SSH on ESXi Hosts in VCF") and "## Resolution" in txt, "export md 형식")
    srv.shutdown()
    blk.shutdown()
    return errors


if __name__ == "__main__":
    print("brcm-docs e2e")
    errs = run()
    print("결과:", "정상" if not errs else f"실패 {len(errs)}건")
    sys.exit(1 if errs else 0)
