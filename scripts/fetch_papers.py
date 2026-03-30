#!/usr/bin/env python3
"""
Daily genomic literature summarizer.
Fetches papers from PubMed (IF>10), bioRxiv, and medRxiv,
uses Claude to generate structured bilingual summaries,
outputs GitHub Issue markdown, webpage HTML, and email.
"""

import os
import json
import time
import smtplib
import requests
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from xml.etree import ElementTree
from pathlib import Path
import anthropic

# ── Journal List (IF > 10) ────────────────────────────────────────────────────
HIGH_IF_JOURNALS = [
    "Nature", "Science", "Cell",
    "N Engl J Med", "Lancet", "JAMA", "BMJ",
    "Nat Med", "Nat Genet", "Nat Neurosci", "Nat Hum Behav",
    "Nat Commun", "Nat Rev Genet",
    "Proc Natl Acad Sci U S A",
    "Circulation",
    "JAMA Psychiatry", "Lancet Psychiatry",
    "Mol Psychiatry", "Biol Psychiatry", "Am J Psychiatry",
    "Lancet Digit Health",
    "Genome Biol", "Genome Med",
    "Cell Genom", "Cell Rep Med",
    "PLoS Med",
]

# ── Search Terms ──────────────────────────────────────────────────────────────
TOPIC1 = '(UKB[tiab] OR "UK Biobank"[tiab] OR "All of Us"[tiab])'
TOPIC2 = '(suicid*[tiab] OR "self-harm"[tiab] OR "self-injury"[tiab])'
TOPIC3 = (
    '(("genome-wide association"[tiab] OR GWAS[tiab] OR TWAS[tiab] OR '
    '"transcriptome-wide association"[tiab]) AND '
    '("single-cell"[tiab] OR "single nucleus"[tiab] OR "scRNA-seq"[tiab] OR '
    '"spatial transcriptomics"[tiab] OR "polygenic risk score"[tiab] OR PRS[tiab]))'
)
TOPIC4 = (
    '(risk[tiab] AND ("machine learning"[tiab] OR '
    '"deep learning"[tiab] OR predict*[tiab]))'
)
PREPRINT_KEYWORDS = [
    "ukb", "uk biobank", "all of us",
    "suicid", "self-harm", "self-injury",
    "gwas", "genome-wide association", "twas", "transcriptome-wide",
    "single-cell", "single nucleus", "scrna-seq",
    "spatial transcriptomics", "polygenic risk", "prs",
    "machine learning", "deep learning",
]


# ── PubMed ────────────────────────────────────────────────────────────────────
def fetch_pubmed_papers():
    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    today = datetime.utcnow()
    start = (today - timedelta(days=2)).strftime("%Y/%m/%d")
    end = today.strftime("%Y/%m/%d")

    journal_filter = " OR ".join([f'"{j}"[Journal]' for j in HIGH_IF_JOURNALS])
    query = (
        f"({TOPIC1} OR {TOPIC2} OR {TOPIC3} OR {TOPIC4}) "
        f"AND ({journal_filter}) "
        f'AND ("{start}"[PDAT]:"{end}"[PDAT])'
    )

    params = {"db": "pubmed", "term": query, "retmax": 50,
              "retmode": "json", "sort": "relevance"}
    ncbi_key = os.environ.get("NCBI_API_KEY", "")
    if ncbi_key:
        params["api_key"] = ncbi_key

    try:
        resp = requests.get(f"{base_url}esearch.fcgi", params=params, timeout=30)
        resp.raise_for_status()
        ids = resp.json().get("esearchresult", {}).get("idlist", [])
    except Exception as e:
        print(f"PubMed search error: {e}")
        return []

    if not ids:
        return []

    time.sleep(0.4)
    fetch_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"}
    if ncbi_key:
        fetch_params["api_key"] = ncbi_key

    try:
        r = requests.get(f"{base_url}efetch.fcgi", params=fetch_params, timeout=30)
        r.raise_for_status()
        return parse_pubmed_xml(r.text)
    except Exception as e:
        print(f"PubMed fetch error: {e}")
        return []


def parse_pubmed_xml(xml_text):
    papers = []
    try:
        root = ElementTree.fromstring(xml_text)
    except Exception as e:
        print(f"XML parse error: {e}")
        return []

    for article in root.findall(".//PubmedArticle"):
        try:
            title_elem = article.find(".//ArticleTitle")
            title = "".join(title_elem.itertext()) if title_elem is not None else "N/A"

            abstract_parts = article.findall(".//AbstractText")
            abstract = " ".join(
                "".join(p.itertext()) for p in abstract_parts
                if "".join(p.itertext()).strip()
            )

            journal_elem = (article.find(".//Journal/ISOAbbreviation")
                            or article.find(".//Journal/Title"))
            journal = journal_elem.text if journal_elem is not None else "N/A"

            pmid_elem = article.find(".//PMID")
            pmid = pmid_elem.text if pmid_elem is not None else ""

            authors = []
            for author in article.findall(".//Author")[:3]:
                last = author.find("LastName")
                if last is not None and last.text:
                    authors.append(last.text)
            author_str = ", ".join(authors) + (" et al." if len(authors) >= 3 else "")

            doi = ""
            for id_elem in article.findall(".//ArticleId"):
                if id_elem.get("IdType") == "doi":
                    doi = id_elem.text or ""
                    break

            papers.append({
                "title": title,
                "abstract": abstract[:600] if abstract else "No abstract available.",
                "journal": journal, "pmid": pmid, "authors": author_str,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "source": "PubMed", "doi": doi,
            })
        except Exception:
            continue
    return papers


# ── Preprints ─────────────────────────────────────────────────────────────────
def fetch_preprints(server="biorxiv"):
    today = datetime.utcnow()
    start = (today - timedelta(days=2)).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    url = f"https://api.biorxiv.org/details/{server}/{start}/{end}/0/json"

    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        collection = resp.json().get("collection", [])
    except Exception as e:
        print(f"{server} fetch error: {e}")
        return []

    papers = []
    for item in collection:
        text = (item.get("title", "") + " " + item.get("abstract", "")).lower()
        if any(kw in text for kw in PREPRINT_KEYWORDS):
            doi = item.get("doi", "")
            papers.append({
                "title": item.get("title", "N/A"),
                "abstract": item.get("abstract", "")[:600],
                "journal": server.capitalize(),
                "authors": item.get("authors", "N/A"),
                "url": f"https://doi.org/{doi}" if doi else "N/A",
                "source": server, "doi": doi,
            })
    return papers[:20]


# ── Claude Summarization (returns structured JSON) ────────────────────────────
def summarize_with_claude(papers, date_str):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    if not papers:
        return {"date": date_str, "papers": [],
                "synthesis": "今日未发现相关新文献。\n\nNo relevant new papers found today."}

    papers_text = "\n\n".join(
        f"[{i}] {p['title']}\n"
        f"  Source: {p['source']} | Journal: {p['journal']}\n"
        f"  Authors: {p.get('authors', 'N/A')}\n"
        f"  URL: {p['url']}\n"
        f"  Abstract: {p['abstract']}"
        for i, p in enumerate(papers, 1)
    )

    prompt = f"""You are an expert genomics research assistant. Today is {date_str}.

Research focus: suicide/self-harm genomics using UKB/All of Us data, GWAS, TWAS,
single-cell RNA-seq, spatial transcriptomics, PRS, and ML/DL risk prediction.

Evaluate {len(papers)} papers and return ONLY a valid JSON object (no markdown, no explanation):

{{
  "date": "{date_str}",
  "papers": [
    {{
      "rank": 1,
      "title": "original English title",
      "cn_title": "中文标题翻译",
      "source": "PubMed or biorxiv or medrxiv",
      "journal": "journal name",
      "authors": "Author et al.",
      "url": "https://...",
      "cn_findings": "2-3句中文核心发现",
      "en_findings": "1-2 sentences in English",
      "relevance": 5
    }}
  ],
  "synthesis": "4-6句中文综合总结，概括今日文献的整体趋势和亮点"
}}

Rules:
- Include only TOP 10 most relevant papers
- relevance: integer 1-5 (5 = directly about suicide genomics with UKB/ML)
- synthesis: Chinese only, 4-6 sentences
- Return ONLY valid JSON

Papers to evaluate:
{papers_text}"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        return {"date": date_str, "papers": [], "synthesis": raw}


# ── Renderers ─────────────────────────────────────────────────────────────────
def render_markdown(data, counts):
    date_str = data["date"]
    papers = data.get("papers", [])
    synthesis = data.get("synthesis", "")
    STARS = {1: "⭐", 2: "⭐⭐", 3: "⭐⭐⭐", 4: "⭐⭐⭐⭐", 5: "⭐⭐⭐⭐⭐"}

    lines = [
        "## 搜索范围 | Search Scope", "",
        "| 来源 Source | 数量 Count |",
        "|-------------|------------|"]
    lines += [
        f"| PubMed (IF > 10) | {counts['pubmed']} |",
        f"| bioRxiv | {counts['biorxiv']} |",
        f"| medRxiv | {counts['medrxiv']} |",
        f"| **合计 Total** | **{counts['total']}** |", "",
        "> **搜索主题**: UKB/All of Us | 自杀/自伤基因组学 | GWAS/TWAS/单细胞/空间组学/PRS | 机器学习风险预测",
        "", "---", "",
    ]

    if not papers:
        lines.append("**今日未发现相关新文献。** No relevant papers found today.")
    else:
        lines.append(f"## 精选文献 | Selected Papers ({len(papers)} 篇)\n")
        for p in papers:
            lines += [
                f"### {p['rank']}. {p['title']}",
                f"**中文标题**: {p['cn_title']}",
                f"**来源**: {p['source']} | **期刊**: {p['journal']} | **作者**: {p['authors']}",
                f"**链接**: {p['url']}",
                f"**核心发现**: {p['cn_findings']}",
                f"**Key findings**: {p['en_findings']}",
                f"**相关性**: {STARS.get(p.get('relevance', 3), '⭐⭐⭐')}",
                "",
            ]

    lines += [
        "---", "", "## 今日综述 | Daily Synthesis", "", synthesis, "", "---",
        f"*自动生成 | Auto-generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC*",
    ]
    return "\n".join(lines)


def render_html(data, counts, archive_dates=None):
    date_str = data["date"]
    papers = data.get("papers", [])
    synthesis = data.get("synthesis", "").replace("\n", "<br>")
    archive_dates = sorted(archive_dates or [], reverse=True)

    def badge(source):
        cls = {"PubMed": "pubmed", "biorxiv": "biorxiv", "medrxiv": "medrxiv"}.get(source, "pubmed")
        return f'<span class="badge badge-{cls}">{source}</span>'

    def stars(n):
        return "★" * n + "☆" * (5 - n)

    cards = ""
    if not papers:
        cards = '<p class="no-results">今日未发现相关新文献。No relevant papers found today.</p>'
    else:
        for p in papers:
            cards += f"""
        <article class="paper-card">
          <div class="paper-meta">{badge(p['source'])} <strong>{p['journal']}</strong>
            &nbsp;·&nbsp; {p['authors']}
            &nbsp;·&nbsp; <span class="stars">{stars(p.get('relevance', 3))}</span>
          </div>
          <h3 class="paper-title">
            <a href="{p['url']}" target="_blank" rel="noopener">{p['rank']}. {p['title']}</a>
          </h3>
          <div class="paper-cn-title">{p['cn_title']}</div>
          <div class="paper-findings">
            <strong>核心发现：</strong>{p['cn_findings']}<br>
            <strong>Key findings:</strong> {p['en_findings']}
          </div>
        </article>"""

    archive_nav = ""
    if archive_dates:
        links = "".join(
            f'<a href="{"index.html" if d == date_str else f"archives/{d}.html"}" '
            f'class="archive-link{" active" if d == date_str else ""}">{d}</a>'
            for d in archive_dates[:30]
        )
        archive_nav = f"""
      <nav class="archive-nav">
        <div class="archive-nav-title">历史存档 | Archive</div>
        <div class="archive-links">{links}</div>
      </nav>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>基因组文献日报 | {date_str}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
           background: #f5f7fa; color: #2d3748; margin: 0; line-height: 1.65; }}
    .header {{ background: linear-gradient(135deg, #1a365d 0%, #2b6cb0 100%);
               color: white; padding: 28px 40px; }}
    .header h1 {{ font-size: 1.35rem; margin: 0; font-weight: 600; }}
    .header .subtitle {{ font-size: 0.82rem; opacity: 0.85; margin-top: 5px; }}
    .date-badge {{ display: inline-block; background: rgba(255,255,255,0.2);
                   border-radius: 20px; padding: 3px 14px; font-size: 0.82rem; margin-top: 10px; }}
    .container {{ max-width: 920px; margin: 0 auto; padding: 28px 20px; }}
    .stats-bar {{ display: grid; grid-template-columns: repeat(4,1fr); gap: 1px;
                  background: #e2e8f0; border: 1px solid #e2e8f0; border-radius: 10px;
                  overflow: hidden; margin-bottom: 24px; }}
    .stat {{ background: white; padding: 14px 10px; text-align: center; }}
    .stat-number {{ font-size: 1.5rem; font-weight: 700; color: #2b6cb0; }}
    .stat-label {{ font-size: 0.7rem; color: #718096; text-transform: uppercase;
                   letter-spacing: 0.05em; margin-top: 2px; }}
    .section-title {{ font-size: 0.82rem; font-weight: 700; color: #1a365d;
                      border-bottom: 2px solid #2b6cb0; padding-bottom: 7px;
                      margin: 24px 0 14px; text-transform: uppercase; letter-spacing: 0.06em; }}
    .paper-card {{ background: white; border: 1px solid #e2e8f0; border-radius: 10px;
                   padding: 18px 22px; margin-bottom: 12px;
                   transition: box-shadow 0.18s; }}
    .paper-card:hover {{ box-shadow: 0 4px 14px rgba(0,0,0,0.08); }}
    .paper-title {{ font-size: 0.97rem; font-weight: 600; color: #1a365d; margin: 5px 0 3px; }}
    .paper-title a {{ color: inherit; text-decoration: none; }}
    .paper-title a:hover {{ color: #2b6cb0; text-decoration: underline; }}
    .paper-meta {{ font-size: 0.76rem; color: #718096; }}
    .paper-cn-title {{ font-size: 0.86rem; color: #4a5568; font-style: italic;
                       margin: 4px 0 10px; }}
    .paper-findings {{ font-size: 0.86rem; line-height: 1.68; color: #4a5568; }}
    .badge {{ display: inline-block; padding: 1px 8px; border-radius: 4px;
              font-size: 0.68rem; font-weight: 700; margin-right: 5px;
              text-transform: uppercase; vertical-align: middle; }}
    .badge-pubmed {{ background: #dbeafe; color: #1e40af; }}
    .badge-biorxiv {{ background: #dcfce7; color: #166534; }}
    .badge-medrxiv {{ background: #fef3c7; color: #92400e; }}
    .stars {{ color: #f59e0b; font-size: 0.82rem; }}
    .synthesis-box {{ background: white; border-left: 4px solid #2b6cb0;
                      border-radius: 0 10px 10px 0; padding: 18px 22px;
                      font-size: 0.93rem; line-height: 1.78; color: #2d3748;
                      box-shadow: 0 1px 4px rgba(0,0,0,0.05); }}
    .archive-nav {{ background: white; border: 1px solid #e2e8f0; border-radius: 10px;
                    padding: 14px 18px; margin-bottom: 24px; }}
    .archive-nav-title {{ font-size: 0.72rem; color: #718096; text-transform: uppercase;
                          letter-spacing: 0.05em; margin-bottom: 9px; font-weight: 600; }}
    .archive-links {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .archive-link {{ padding: 3px 11px; background: #f7fafc; border: 1px solid #e2e8f0;
                     border-radius: 5px; font-size: 0.76rem; text-decoration: none;
                     color: #4a5568; transition: all 0.15s; }}
    .archive-link:hover, .archive-link.active {{
      background: #2b6cb0; color: white; border-color: #2b6cb0; }}
    .no-results {{ color: #718096; font-style: italic; }}
    footer {{ text-align: center; font-size: 0.76rem; color: #a0aec0;
              padding: 28px 20px; border-top: 1px solid #e2e8f0; margin-top: 44px; }}
    footer a {{ color: #a0aec0; }}
    @media (max-width: 600px) {{
      .header {{ padding: 18px 20px; }}
      .stats-bar {{ grid-template-columns: repeat(2,1fr); }}
    }}
  </style>
</head>
<body>
<header class="header">
  <h1>📚 基因组文献日报 | Genomic Literature Daily</h1>
  <div class="subtitle">UKB · 自杀基因组学 · GWAS / TWAS / 单细胞 / 空间组学 · 机器学习风险预测</div>
  <div class="date-badge">{date_str}</div>
</header>

<div class="container">
  {archive_nav}

  <div class="stats-bar">
    <div class="stat"><div class="stat-number">{counts['pubmed']}</div>
      <div class="stat-label">PubMed IF&gt;10</div></div>
    <div class="stat"><div class="stat-number">{counts['biorxiv']}</div>
      <div class="stat-label">bioRxiv</div></div>
    <div class="stat"><div class="stat-number">{counts['medrxiv']}</div>
      <div class="stat-label">medRxiv</div></div>
    <div class="stat"><div class="stat-number">{len(papers)}</div>
      <div class="stat-label">精选 Selected</div></div>
  </div>

  <div class="section-title">精选文献 | Selected Papers</div>
  {cards}

  <div class="section-title">今日综述 | Daily Synthesis</div>
  <div class="synthesis-box">{synthesis}</div>
</div>

<footer>
  自动生成 · Auto-generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC ·
  <a href="https://github.com/loveyu3317/genomic-literature-summary">GitHub</a>
</footer>
</body>
</html>"""


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(subject, html_content, text_content):
    sender = os.environ.get("GMAIL_USER", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    recipient = os.environ.get("EMAIL_RECIPIENT", sender)

    if not sender or not password:
        print("Email skipped: GMAIL_USER or GMAIL_APP_PASSWORD not set.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"文献日报 <{sender}>"
    msg["To"] = recipient
    msg.attach(MIMEText(text_content, "plain", "utf-8"))
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        print(f"Email sent to {recipient}")
    except Exception as e:
        print(f"Email error: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    print(f"=== Daily Literature Summary: {date_str} ===")

    pubmed_papers = fetch_pubmed_papers()
    print(f"PubMed: {len(pubmed_papers)}")
    biorxiv_papers = fetch_preprints("biorxiv")
    print(f"bioRxiv: {len(biorxiv_papers)}")
    medrxiv_papers = fetch_preprints("medrxiv")
    print(f"medRxiv: {len(medrxiv_papers)}")

    all_papers = pubmed_papers + biorxiv_papers + medrxiv_papers
    counts = {
        "pubmed": len(pubmed_papers), "biorxiv": len(biorxiv_papers),
        "medrxiv": len(medrxiv_papers), "total": len(all_papers),
    }
    print(f"Total: {len(all_papers)} → sending to Claude...")

    data = summarize_with_claude(all_papers, date_str)

    # Markdown for GitHub Issue
    markdown_output = render_markdown(data, counts)
    with open("daily_summary.md", "w", encoding="utf-8") as f:
        f.write(markdown_output)
    print("Saved daily_summary.md")

    # HTML for webpage
    docs_dir = Path("docs")
    archives_dir = docs_dir / "archives"
    archives_dir.mkdir(parents=True, exist_ok=True)

    archive_dates = [p.stem for p in archives_dir.glob("*.html")]
    if date_str not in archive_dates:
        archive_dates.append(date_str)

    html_content = render_html(data, counts, archive_dates)

    archive_path = archives_dir / f"{date_str}.html"
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"Saved {archive_path}")

    with open(docs_dir / "index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("Updated docs/index.html")

    # Send email
    subject = f"\U0001f4da 文献日报 {date_str} | {len(data.get('papers', []))} 篇精选"
    send_email(subject, html_content, markdown_output)


if __name__ == "__main__":
    main()
