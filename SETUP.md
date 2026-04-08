# Setup & Deployment

## ⚠️ IMPORTANT - SECURITY

**NEVER commit the following to GitHub:**
- `.env` (in .gitignore ✓)
- `credentials.json` (in .gitignore ✓)
- `token.json` (in .gitignore ✓)
- `service_account.json` (in .gitignore ✓)

## Local Setup

```powershell
# 1. Create virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -e .

# 3. Copy template and fill values
cp .env.example .env
# Edit .env with your real values (API keys, project ID, etc)

# 4. Start database
docker-compose up -d

# 5. Apply schema
psql -h localhost -U postgres -d crisiscommand -f database/schema.sql

# 6. Run webhook
uvicorn webhook.main:app --host 0.0.0.0 --port 8000
```

## Production Deployment

```powershell
# Single command deploys everything securely
# (reads from .env, stores secrets in Google Secret Manager)
.\deploy-secure.ps1
```

That's it. Secrets are encrypted. No hardcoded values. GitHub-safe.

## Files NOT to Edit

- `.env.example` - Template only, use this to create `.env`
- `.gitignore` - Do not modify

## Verify Security Before Pushing

```powershell
# Check nothing sensitive will be committed
git status

# Should NOT show: .env, *.json, *.key files

# If you see them, run:
git rm --cached .env credentials.json token.json service_account.json
git commit -m "Remove sensitive files"
```

Then push safely:
```powershell
git push origin main
```
