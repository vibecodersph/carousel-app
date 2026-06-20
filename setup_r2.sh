#!/bin/bash
# R2 Bucket Setup for Carousel App Instagram Publishing
# 
# Prerequisites:
#   1. Cloudflare account with R2 enabled
#   2. wrangler CLI installed: npm install -g wrangler
#
# Usage:
#   chmod +x setup_r2.sh
#   ./setup_r2.sh

set -e

BUCKET_NAME="${R2_BUCKET:-llmaw-carousel-media}"
REGION="${R2_REGION:-apac}"

echo "=== Carousel App R2 Setup ==="
echo ""

# Check wrangler
if ! command -v wrangler &> /dev/null; then
    echo "[!] wrangler not found. Install with: npm install -g wrangler"
    echo "    Then run: wrangler login"
    exit 1
fi

# Check if logged in
if ! wrangler whoami &> /dev/null; then
    echo "[!] Not logged into Cloudflare. Run: wrangler login"
    exit 1
fi

echo "[*] Creating R2 bucket: $BUCKET_NAME"
wrangler r2 bucket create "$BUCKET_NAME" --location "$REGION" 2>/dev/null || echo "    (bucket may already exist)"

echo ""
echo "[*] Bucket info:"
wrangler r2 bucket list | grep "$BUCKET_NAME" || echo "    Could not verify"

echo ""
echo "[*] To allow public access, create a custom domain or use r2.dev:"
echo "    1. Go to Cloudflare Dashboard > R2 > $BUCKET_NAME > Settings"
echo "    2. Enable 'r2.dev' subdomain (for testing)"
echo "    3. Or connect a custom domain"
echo ""
echo "[*] Get API tokens:"
echo "    1. Go to https://dash.cloudflare.com/profile/api-tokens"
echo "    2. Create token with: R2 Read & Write permission"
echo "    3. Scope to: $BUCKET_NAME"
echo ""
echo "[*] Add to your carousel-app .env:"
echo "    R2_ACCOUNT_ID=\$(wrangler whoami | jq -r .account_id)"
echo "    R2_ACCESS_KEY_ID=<your-access-key-id>"
echo "    R2_SECRET_ACCESS_KEY=<your-secret-access-key>"
echo "    R2_BUCKET=$BUCKET_NAME"

# Print account ID if available
ACCOUNT_ID=$(wrangler whoami 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('account_id',''))" 2>/dev/null || echo "")
if [ -n "$ACCOUNT_ID" ]; then
    echo ""
    echo "    Your R2_ACCOUNT_ID: $ACCOUNT_ID"
fi
