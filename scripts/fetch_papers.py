#!/usr/bin/env python3
"""
Daily genomic literature summarizer.
Fetches papers from PubMed (IF>10), bioRxiv, and medRxiv,
then uses Claude to generate a bilingual (Chinese/English) summary.
"""

import os
import time
import requests
from datetime import datetime, timedelta
from xml.etree import ElementTree
import anthropic

# High-impact journals (IF > 10) relevant to genomics / psychiatry / medicine
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

# Search topics (OR logic between topics)
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


def get_date_range(days_back=2):
    today = datetime.utcnow()
    start = today - timedelta(days=days_back)
    return start.strftime("%Y/%m/%d"), today.strftime("%Y/%m/%d")


def fetch_pubmed_papers():
    """Fetch recent papers from PubMed filtered by high-IF journals."""
    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    start_date, end_date = get_date_range()

    journal_filter = " OR ".join([f'"{j}"[Journal]' for j in HIGH_IF_JOURNALS])
    query = (
        f"({TOPIC1} OR {TOPIC2} OR {TOPIC3} OR {TOPIC4}) "
        f"AND ({journal_filter}) "
        f'AND ("{start_date}"[PDAT]:"{end_date}"[PDAT])'
    )

    search_params = {
        "db": "pubmed",
        "term": query,
        "retmax": 50,
        "retmode": "json",
        "sort": "relevance",
    }
    ncbi_key = os.environ.get("NCBI_API_KEY", "")
    if ncbi_key:
        search_params["api_key"] = ncbi_key

    try:
        resp = requests.get(
            f"{base_url}esearch.fcgi", params=search_params, timeout=30
        )
        resp.raise_for_status()
        ids = resp.json().get("esearchresult", {}).get("idlist", [])
    except Exception as e:
        print(f"PubMed search error: {e}")
        return []

    if not ids:
        print("PubMed: no IDs found.")
        return []

    time.sleep(0.4)  # Respect NCBI rate limit

    fetch_params = {"db": "pubmed", "id": ",".join(ids), "retmode": "xml"}
    if ncbi_key:
        fetch_params["api_key"] = ncbi_key

    try:
        fetch_resp = requests.get(
            f"{base_url}efetch.fcgi", params=fetch_params, timeout=30
        )
        fetch_resp.raise_for_status()
        return parse_pubmed_xml(fetch_resp.text)
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
            title = (
                "".join(title_elem.itertext()) if title_elem is not None else "N/A"
            )

            abstract_parts = article.findall(".//AbstractText")
            abstract = " ".join(
                "".join(p.itertext())
                for p in abstract_parts
                if "".join(p.itertext()).strip()
            )

            journal_elem = article.find(".//Journal/ISOAbbreviation")
            if journal_elem is None:
                journal_elem = article.find(".//Journal/Title")
            journal = journal_elem.text if journal_elem is not None else "N/A"

            pmid_elem = article.find(".//PMID")
            pmid = pmid_elem.text if pmid_elem is not None else ""

            authors = []
            for author in article.findall(".//Author")[:3]:
                last = author.find("LastName")
                if last is not None and last.text:
                    authors.append(last.text)
            author_str = ", ".join(authors) + (
                " et al." if len(authors) >= 3 else ""
            )

            doi = ""
            for id_elem in article.findall(".//ArticleId"):
                if id_elem.get("IdType") == "doi":
                    doi = id_elem.text or ""
                    break

            papers.append({
                "title": title,
                "abstract": abstract[:600] if abstract else "No abstract available.",
                "journal": journal,
                "pmid": pmid,
                "authors": author_str,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "source": "PubMed",
                "doi": doi,
            })
        except Exception:
            continue

    return papers


def fetch_preprints(server="biorxiv"):
    """Fetch preprints from bioRxiv or medRxiv using keyword filtering."""
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
        text = (
            item.get("title", "") + " " + item.get("abstract", "")
        ).lower()
        if any(kw in text for kw in PREPRINT_KEYWORDS):
            doi = item.get("doi", "")
            papers.append({
                "title": item.get("title", "N/A"),
                "abstract": item.get("abstract", "")[:600],
                "journal": server.capitalize(),
                "authors": item.get("authors", "N/A"),
                "url": f"https://doi.org/{doi}" if doi else "N/A",
                "source": server,
                "doi": doi,
            })

    return papers[:20]


def summarize_with_claude(papers, date_str):
    """Use Claude to filter, rank, and summarize papers bilingually."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    if not papers:
        return "**今日未发现相关新文献。**\n\nNo relevant new papers found today."

    papers_text = "\n\n".join(
        f"[{i}] **{p['title']}**\n"
        f"  - Source: {p['source']} | Journal: {p['journal']}\n"
        f"  - Authors: {p.get('authors', 'N/A')}\n"
        f"  - URL: {p['url']}\n"
        f"  - Abstract: {p['abstract']}"
        for i, p in enumerate(papers, 1)
    )

    prompt = f"""You are an expert genomics research assistant helping a researcher focused on:
- Suicide / self-harm genomics using large biobank data (UKB, All of Us)
- GWAS, TWAS, single-cell RNA-seq, spatial transcriptomics
- Polygenic risk scores (PRS) for suicidal behavior
- Machine learning / deep learning for psychiatric risk prediction

Today is {date_str}. I have collected {len(papers)} papers from PubMed (IF>10), medRxiv, and bioRxiv.

**Your tasks:**
1. Identify and rank the TOP 10 most relevant papers (must relate to the research topics above).
2. For each selected paper, provide a structured entry in this exact format:

### [Rank]. [English Title]
**中文标题**: [Chinese translation of title]  
**来源**: [Journal] | **作者**: [First author et al.]  
**链接**: [URL]  
**核心发现**: [2-3 sentences in Chinese explaining key findings and significance]  
**Key findings**: [1-2 sentences in English]  
**相关性**: [⭐ to ⭐⭐⭐⭐⭐, where ⭐⭐⭐⭐⭐ = highly relevant to suicide genomics/UKB/ML]

3. After all papers, add:

## 今日综述 | Daily Synthesis
[4-6 sentences in Chinese summarizing overall trends, methodological advances, and clinical/research implications from today's papers]

---
Papers to evaluate:

{papers_text}

Only include papers that are genuinely relevant. If fewer than 10 are relevant, include only those."""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def main():
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    print(f"=== Daily Literature Summary: {date_str} ===")

    pubmed_papers = fetch_pubmed_papers()
    print(f"PubMed: {len(pubmed_papers)} papers")

    biorxiv_papers = fetch_preprints("biorxiv")
    print(f"bioRxiv: {len(biorxiv_papers)} papers")

    medrxiv_papers = fetch_preprints("medrxiv")
    print(f"medRxiv: {len(medrxiv_papers)} papers")

    all_papers = pubmed_papers + biorxiv_papers + medrxiv_papers
    print(f"Total: {len(all_papers)} papers — sending to Claude...")

    summary = summarize_with_claude(all_papers, date_str)

    output = f"""## 搜索范围 | Search Scope

| 来源 Source | 数量 Count |
|-------------|------------|
| PubMed (IF > 10) | {len(pubmed_papers)} |
| bioRxiv | {len(biorxiv_papers)} |
| medRxiv | {len(medrxiv_papers)} |
| **合计 Total** | **{len(all_papers)}** |

> **搜索主题**: UKB / All of Us | 自杀/自伤基因组学 | GWAS / TWAS / 单细胞 / 空间组学 / PRS | 机器学习风险预测

---

{summary}

---
*自动生成 | Auto-generated at {datetime.utcnow().strftime("%Y-%m-%d %H:%M")} UTC*
"""

    with open("daily_summary.md", "w", encoding="utf-8") as f:
        f.write(output)
    print("Saved to daily_summary.md")


if __name__ == "__main__":
    main()
