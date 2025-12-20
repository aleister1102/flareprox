# FlareProx

IP rotation & URL redirection via Cloudflare Workers. Supports all HTTP methods with 100k free requests/day.

> Fork of [MrTurvey/flareprox](https://github.com/MrTurvey/flareprox)

## Setup

```bash
pip install -r requirements.txt
```

Configure credentials via `python3 flareprox.py config` or edit `flareprox.json`:

```json
{
  "cloudflare": {
    "api_token": "your_cloudflare_api_token",
    "account_id": "your_cloudflare_account_id"
  }
}
```

Get your credentials from [Cloudflare API Tokens](https://dash.cloudflare.com/profile/api-tokens) using the "Edit Cloudflare Workers" template.

## Usage

```bash
# Create proxy endpoints
python3 flareprox.py create --count 2

# List endpoints
python3 flareprox.py list

# Test endpoints
python3 flareprox.py test

# Cleanup
python3 flareprox.py cleanup
```

### Making Requests

```bash
# Via query parameter
curl "https://your-worker.account.workers.dev?url=https://httpbin.org/ip"

# Via custom header
curl -H "X-Target-URL: https://httpbin.org/ip" https://your-worker.account.workers.dev
```

### Programmatic Usage

```python
from flareprox import FlareProx

flareprox = FlareProx(config_file="flareprox.json")
endpoints = flareprox.sync_endpoints()

response = flareprox.redirect_request(
    target_url="https://httpbin.org/get",
    method="GET"
)
```
