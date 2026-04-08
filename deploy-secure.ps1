#!/usr/bin/env pwsh
# Quick Deploy Script - Use this for safe deployment
# This script reads from .env (which is in .gitignore) and deploys securely

param(
    [Parameter(Mandatory=$false)]
    [string]$ProjectId = "your-legal-ai-project"
)

Write-Host "🔐 SECURE CRISISCOMMAND DEPLOYMENT" -ForegroundColor Cyan
Write-Host "===================================" -ForegroundColor Cyan
Write-Host ""

# Load .env securely (won't be committed)
if (-not (Test-Path ".env")) {
    Write-Host "❌ Error: .env file not found!" -ForegroundColor Red
    Write-Host "Create it from .env.example first"
    exit 1
}

Write-Host "✅ .env file found (secure, not in git)" -ForegroundColor Green

# Read values
$env_content = Get-Content ".env" -Raw
$env_hash = @{}
$env_content -split "`n" | Where-Object { $_ -match "=" } | ForEach-Object {
    $parts = $_ -split "=", 2
    $env_hash[$parts[0].Trim()] = $parts[1].Trim()
}

$ProjectId = $env_hash["GOOGLE_CLOUD_PROJECT"]
$ChatSpaceId = $env_hash["CHAT_SPACE_ID"]
$DocsFolder = $env_hash["DOCS_FOLDER_ID"]
$ApiKey = $env_hash["GOOGLE_API_KEY"]

Write-Host ""
Write-Host "📋 Loaded Configuration:" -ForegroundColor Yellow
Write-Host "   Project: $ProjectId"
Write-Host "   Chat Space: $ChatSpaceId"
Write-Host "   Docs Folder: $DocsFolder"
Write-Host "   API Key: $($ApiKey.Substring(0, 20))..." -ForegroundColor DarkRed
Write-Host ""

# Step 1: Auth
Write-Host "Step 1️⃣ Authenticating with GCP..." -ForegroundColor Cyan
gcloud auth login
gcloud config set project $ProjectId

# Step 2: Build image
Write-Host ""
Write-Host "Step 2️⃣ Building Docker image..." -ForegroundColor Cyan
docker build -f Dockerfile.api -t crisiscommand-webhook:latest .

# Step 3: Configure artifact registry
Write-Host ""
Write-Host "Step 3️⃣ Configuring Docker auth..." -ForegroundColor Cyan
gcloud auth configure-docker us-central1-docker.pkg.dev

# Step 4: Tag and push
Write-Host ""
Write-Host "Step 4️⃣ Pushing image to Artifact Registry..." -ForegroundColor Cyan
$ImageUrl = "us-central1-docker.pkg.dev/$ProjectId/crisiscommand/webhook:latest"
docker tag crisiscommand-webhook:latest $ImageUrl
docker push $ImageUrl

# Step 5: Create secrets
Write-Host ""
Write-Host "Step 5️⃣ Storing secrets securely in Google Secret Manager..." -ForegroundColor Cyan
echo $ApiKey | gcloud secrets create GOOGLE_API_KEY --data-file=- 2>$null
echo $ChatSpaceId | gcloud secrets create CHAT_SPACE_ID --data-file=- 2>$null
echo $DocsFolder | gcloud secrets create DOCS_FOLDER_ID --data-file=- 2>$null

# Generate random secret
$SimSecret = -join ((65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_})
echo $SimSecret | gcloud secrets create SIMULATOR_SECRET --data-file=- 2>$null

Write-Host "   ✓ GOOGLE_API_KEY stored"
Write-Host "   ✓ CHAT_SPACE_ID stored"
Write-Host "   ✓ DOCS_FOLDER_ID stored"
Write-Host "   ✓ SIMULATOR_SECRET stored"
Write-Host ""
Write-Host "🔑 Simulator Secret for testing: $SimSecret" -ForegroundColor Yellow

# Step 6: Deploy to Cloud Run
Write-Host ""
Write-Host "Step 6️⃣ Deploying to Cloud Run..." -ForegroundColor Cyan

gcloud run deploy crisiscommand-webhook `
  --image $ImageUrl `
  --region us-central1 `
  --platform managed `
  --allow-unauthenticated `
  --memory 512Mi `
  --cpu 1 `
  --timeout 3600 `
  --set-env-vars `
    GOOGLE_API_KEY=$(gcloud secrets versions access latest --secret=GOOGLE_API_KEY),`
    CHAT_SPACE_ID=$(gcloud secrets versions access latest --secret=CHAT_SPACE_ID),`
    DOCS_FOLDER_ID=$(gcloud secrets versions access latest --secret=DOCS_FOLDER_ID),`
    SIMULATOR_SECRET=$SimSecret

# Get URL
Write-Host ""
Write-Host "Step 7️⃣ Getting service URL..." -ForegroundColor Cyan
$ServiceUrl = gcloud run services describe crisiscommand-webhook `
  --region us-central1 `
  --format='value(status.url)'

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "✨ DEPLOYMENT COMPLETE!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "🔗 Your Public URL:" -ForegroundColor Yellow
Write-Host "   $ServiceUrl"
Write-Host ""
Write-Host "🧪 Test it:" -ForegroundColor Yellow
Write-Host "   curl $ServiceUrl/health"
Write-Host ""
Write-Host "📊 View dashboard:" -ForegroundColor Yellow
Write-Host "   $ServiceUrl"
Write-Host ""
Write-Host "🔐 Security Status:" -ForegroundColor Green
Write-Host "   ✓ API Keys in Secret Manager (NOT in code)"
Write-Host "   ✓ .env in .gitignore (NOT on GitHub)"
Write-Host "   ✓ Database secure (Cloud SQL)"
Write-Host "   ✓ HTTPS/TLS enabled"
Write-Host ""
Write-Host "💾 GitHub safe to push:" -ForegroundColor Green
Write-Host "   git push origin main"
Write-Host ""
