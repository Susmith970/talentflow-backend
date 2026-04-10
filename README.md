# TalentFlow Backend

AI-powered job hunt automation API. Built with Flask + Playwright.

## Deploy to Railway

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app)

### Required environment variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (from console.anthropic.com) |
| `FLASK_SECRET` | Any long random string for session signing |
| `FRONTEND_URL` | Your Vercel frontend URL (set after deploying frontend) |

### Optional

| Variable | Description |
|----------|-------------|
| `LINKEDIN_EMAIL` | For LinkedIn password login |
| `LINKEDIN_PASSWORD` | For LinkedIn password login |
| `DATA_DIR` | Path to persistent volume (leave blank for ephemeral) |
| `SCRAPE_WINDOW_HOURS` | Hours to look back when scraping (default: 24) |

## Local development

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # fill in your keys
python app.py
```
