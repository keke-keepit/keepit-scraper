# Keepit KB Skill

A knowledge base skill for Cowork that answers questions about Keepit's products, company, and services using content scraped directly from keepit.com and lp.keepit.com.

## For Maintainers

### What the repo does

`build_kb.py` scrapes Keepit's public web content into topic-grouped `.txt` files stored in `kb/upload/`:

- `keepit_blog.txt` — Blog articles
- `keepit_company.txt` — Company information
- `keepit_customers.txt` — Customer stories and case studies
- `keepit_documents.txt` — Product documentation
- `keepit_help.txt` — Help and how-to content
- `keepit_partners.txt` — Partner information
- `keepit_press.txt` — Press releases and news
- `keepit_resources.txt` — Resource library
- `keepit_security.txt` — Security and compliance information
- `keepit_services.txt` — Services and offerings

### Workflows

Two GitHub Actions workflows automate the knowledge base and skill packaging:

**Weekly KB Build** (`.github/workflows/scrape.yml`)
- Runs Sundays at 00:00 UTC
- Executes `python build_kb.py --out kb`
- Commits changes to `kb/` if content has changed
- Opens an issue if the build fails

**Skill Packaging** (`.github/workflows/package-skill.yml`)
- Runs Sundays at 02:00 UTC (after the KB build)
- Assembles `SKILL.md` and the 10 `kb/upload/keepit_*.txt` files into `keepit-kb/`
- Zips as `keepit-kb.zip`
- Publishes to the GitHub `latest-skill` release
- Users always download the current version from the same release URL

### Important: Public Content Only

This repo is public. Only include content from public pages on keepit.com and lp.keepit.com. Do not add internal information, customer data, or anything requiring authentication.

## For End Users

### Installation

1. Go to the **Releases** page and find the `latest-skill` release
2. Download `keepit-kb.zip`
3. In Cowork, go to **Settings > Skills** and select **Import skill**
4. Choose the downloaded file and import

### What it does

Once installed, the skill answers questions about Keepit's products, features, pricing, security, compliance, customers, partners, and company information. Every answer includes a source URL from the knowledge base.

The skill draws only from its bundled knowledge base — it does not have internet access and will not answer questions outside of Keepit's documented public content.

### Keeping it fresh

The knowledge base is updated weekly. To get the latest content, periodically re-download `keepit-kb.zip` from Releases and re-import it in Cowork. You may want to set a calendar reminder every two weeks.

## Development

### Requirements

- Python 3.12+
- Dependencies: `requests`, `trafilatura`, `pymupdf` (see `requirements.txt`)

### Manual build

```bash
pip install -r requirements.txt
python build_kb.py --out kb
```

Output is stored in `kb/upload/` by topic.
