# Genomic Literature Summary

Automatic daily literature digest focused on:
- **UKB / UK Biobank / All of Us** data studies
- **Suicide / self-harm** genomics
- **GWAS, TWAS, single-cell, spatial transcriptomics, PRS**
- **Machine learning / deep learning** for psychiatric risk prediction

## Sources
| Source | Filter |
|--------|--------|
| PubMed | Impact Factor > 10 journals only |
| bioRxiv | Keyword-filtered |
| medRxiv | Keyword-filtered |

## Schedule
Runs automatically every day at **9:30 AM ET** (Eastern Time) via GitHub Actions.

Results are posted as GitHub Issues with the `daily-summary` label.

## Setup

### Required GitHub Secrets
| Secret | Description |
|--------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude summarization |
| `NCBI_API_KEY` | *(Optional)* NCBI API key for higher PubMed rate limits |

### Manual Run
Go to **Actions** → **Daily Literature Summary** → **Run workflow**

## Output Format
Each daily Issue contains:
1. Search statistics (papers found per source)
2. Top 10 most relevant papers with:
   - Chinese title translation
   - Key findings in Chinese + English
   - Relevance rating (1–5 stars)
3. Daily synthesis paragraph in Chinese
