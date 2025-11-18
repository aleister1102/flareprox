#!/usr/bin/env python3
"""
FlareProx - Simple URL Redirection via Cloudflare Workers
Redirect all traffic through Cloudflare Workers for any provided URL
"""

import argparse
import http.server
import getpass
import json
import os
import random
import requests
import string
import sys
import time
import subprocess
import signal
import socket
import select
from urllib.parse import urlparse, parse_qs, quote
from typing import Dict, List, Optional


class FlareProxError(Exception):
    """Custom exception for FlareProx-specific errors."""
    pass


class CloudflareManager:
    """Manages Cloudflare Worker deployments for FlareProx."""

    def __init__(self, api_token: str, account_id: str, zone_id: Optional[str] = None):
        self.api_token = api_token
        self.account_id = account_id
        self.zone_id = zone_id
        self.base_url = "https://api.cloudflare.com/client/v4"
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
        self._account_subdomain = None

    def _generate_subdomain_name(self) -> str:
        """Generate a subdomain name for new accounts."""
        # Use first 10 chars of account ID + 3 random chars
        account_prefix = self.account_id[:10].lower()
        random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=3))
        return f"{account_prefix}-{random_suffix}"

    def ensure_subdomain_provisioned(self) -> str:
        """Provision a workers.dev subdomain for the account if it doesn't exist."""
        url = f"{self.base_url}/accounts/{self.account_id}/workers/subdomain"

        # First, check if subdomain already exists
        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            if response.status_code == 200:
                data = response.json()
                subdomain = data.get("result", {}).get("subdomain")
                if subdomain:
                    return subdomain
        except requests.RequestException:
            pass

        # Subdomain doesn't exist, create it
        subdomain_name = self._generate_subdomain_name()

        try:
            response = requests.put(
                url,
                headers=self.headers,
                json={"subdomain": subdomain_name},
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                subdomain = data.get("result", {}).get("subdomain")
                if subdomain:
                    print(f"\n  ✓ Subdomain provisioned: {subdomain}.workers.dev\n")
                    return subdomain
            elif response.status_code == 409:
                # Error 10036: subdomain already exists
                # This can happen if subdomain was created between our GET and PUT
                # Try GET again to retrieve it
                response = requests.get(url, headers=self.headers, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    subdomain = data.get("result", {}).get("subdomain")
                    if subdomain:
                        return subdomain

                raise FlareProxError(
                    "Subdomain already exists but couldn't retrieve it. "
                    "Please visit https://dash.cloudflare.com -> Workers & Pages"
                )
            else:
                error_data = response.json() if response.content else {}
                errors = error_data.get("errors", [])
                error_msg = errors[0].get("message", "Unknown error") if errors else "Unknown error"
                raise FlareProxError(
                    f"Failed to provision workers.dev subdomain (HTTP {response.status_code}): {error_msg}. "
                    "Please ensure your API token has 'Workers Scripts:Write' permission."
                )
        except requests.RequestException as e:
            raise FlareProxError(
                f"Network error while provisioning subdomain: {e}"
            )

    @property
    def worker_subdomain(self) -> str:
        """Get the worker subdomain for workers.dev URLs."""
        if self._account_subdomain:
            return self._account_subdomain

        # Try to get configured subdomain with retries
        url = f"{self.base_url}/accounts/{self.account_id}/workers/subdomain"
        max_retries = 3

        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=self.headers, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    subdomain = data.get("result", {}).get("subdomain")
                    if subdomain:
                        self._account_subdomain = subdomain
                        return subdomain
                    else:
                        # API succeeded but returned empty subdomain
                        if attempt < max_retries - 1:
                            time.sleep(1)  # Wait before retry
                            continue
                        raise FlareProxError(
                            "Cloudflare API returned no workers.dev subdomain. "
                            "Your account should have a default subdomain assigned. "
                            "Please check https://dash.cloudflare.com -> Workers & Pages"
                        )
                elif response.status_code == 404:
                    # Subdomain not provisioned yet - try to provision it automatically
                    if attempt == 0:
                        print("\n  ⚙ Setting up workers.dev subdomain for your account...")
                        try:
                            subdomain = self.ensure_subdomain_provisioned()
                            if subdomain:
                                self._account_subdomain = subdomain
                                return subdomain
                        except FlareProxError as e:
                            # If provisioning fails, retry with GET in next attempt
                            if attempt < max_retries - 1:
                                time.sleep(2)
                                continue
                            raise
                    else:
                        # Already tried provisioning, just wait and retry
                        if attempt < max_retries - 1:
                            time.sleep(2)
                            continue
                        raise FlareProxError(
                            "Workers subdomain could not be provisioned automatically. "
                            "Please visit https://dash.cloudflare.com -> Workers & Pages to initialize your account."
                        )
                else:
                    raise FlareProxError(
                        f"Failed to retrieve workers.dev subdomain (HTTP {response.status_code}). "
                        "Please check your API token has 'Workers Scripts:Read' permission."
                    )
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                raise FlareProxError(
                    f"Network error while retrieving workers.dev subdomain: {e}"
                )

        # Should never reach here due to raises above, but just in case
        raise FlareProxError("Failed to retrieve workers.dev subdomain after retries")

    def _generate_worker_name(self) -> str:
        """Generate a unique worker name."""
        timestamp = str(int(time.time()))
        random_suffix = ''.join(random.choices(string.ascii_lowercase, k=6))
        return f"flareprox-{timestamp}-{random_suffix}"

    def _get_worker_script(self) -> str:
        """Return the optimized Cloudflare Worker script."""
        return '''/**
 * FlareProx - Cloudflare Worker URL Redirection Script
 */
addEventListener('fetch', event => {
  event.respondWith(handleRequest(event.request))
})

async function handleRequest(request) {
  try {
    const url = new URL(request.url)
    const targetUrl = getTargetUrl(url, request.headers)

    if (!targetUrl) {
      return createErrorResponse('No target URL specified', {
        usage: {
          query_param: '?url=https://example.com',
          header: 'X-Target-URL: https://example.com',
          path: '/https://example.com'
        }
      }, 400)
    }

    let targetURL
    try {
      targetURL = new URL(targetUrl)
    } catch (e) {
      return createErrorResponse('Invalid target URL', { provided: targetUrl }, 400)
    }

    // Build target URL with filtered query parameters
    const targetParams = new URLSearchParams()
    for (const [key, value] of url.searchParams) {
      if (!['url', '_cb', '_t'].includes(key)) {
        targetParams.append(key, value)
      }
    }
    if (targetParams.toString()) {
      targetURL.search = targetParams.toString()
    }

    // Create proxied request
    const proxyRequest = createProxyRequest(request, targetURL)
    const response = await fetch(proxyRequest)

    // Process and return response
    return createProxyResponse(response, request.method)

  } catch (error) {
    return createErrorResponse('Proxy request failed', {
      message: error.message,
      timestamp: new Date().toISOString()
    }, 500)
  }
}

function getTargetUrl(url, headers) {
  // Priority: query param > header > path
  let targetUrl = url.searchParams.get('url')

  if (!targetUrl) {
    targetUrl = headers.get('X-Target-URL')
  }

  if (!targetUrl && url.pathname !== '/') {
    const pathUrl = url.pathname.slice(1)
    if (pathUrl.startsWith('http')) {
      targetUrl = pathUrl
    }
  }

  return targetUrl
}

function createProxyRequest(request, targetURL) {
  const proxyHeaders = new Headers()
  const allowedHeaders = [
    'accept', 'accept-language', 'accept-encoding', 'authorization',
    'cache-control', 'content-type', 'origin', 'referer', 'user-agent'
  ]

  // Copy allowed headers
  for (const [key, value] of request.headers) {
    if (allowedHeaders.includes(key.toLowerCase())) {
      proxyHeaders.set(key, value)
    }
  }

  proxyHeaders.set('Host', targetURL.hostname)

  // Set X-Forwarded-For header
  const customXForwardedFor = request.headers.get('X-My-X-Forwarded-For')
  if (customXForwardedFor) {
    proxyHeaders.set('X-Forwarded-For', customXForwardedFor)
  } else {
    proxyHeaders.set('X-Forwarded-For', generateRandomIP())
  }

  return new Request(targetURL.toString(), {
    method: request.method,
    headers: proxyHeaders,
    body: ['GET', 'HEAD'].includes(request.method) ? null : request.body
  })
}

function createProxyResponse(response, requestMethod) {
  const responseHeaders = new Headers()

  // Copy response headers (excluding problematic ones)
  for (const [key, value] of response.headers) {
    if (!['content-encoding', 'content-length', 'transfer-encoding'].includes(key.toLowerCase())) {
      responseHeaders.set(key, value)
    }
  }

  // Add CORS headers
  responseHeaders.set('Access-Control-Allow-Origin', '*')
  responseHeaders.set('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS, PATCH, HEAD')
  responseHeaders.set('Access-Control-Allow-Headers', '*')

  if (requestMethod === 'OPTIONS') {
    return new Response(null, { status: 204, headers: responseHeaders })
  }

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders
  })
}

function createErrorResponse(error, details, status) {
  return new Response(JSON.stringify({ error, ...details }), {
    status,
    headers: { 'Content-Type': 'application/json' }
  })
}

function generateRandomIP() {
  return [1, 2, 3, 4].map(() => Math.floor(Math.random() * 255) + 1).join('.')
}'''

    def create_deployment(self, name: Optional[str] = None) -> Dict:
        """Deploy a new Cloudflare Worker."""
        if not name:
            name = self._generate_worker_name()

        script_content = self._get_worker_script()
        url = f"{self.base_url}/accounts/{self.account_id}/workers/scripts/{name}"

        files = {
            'metadata': (None, json.dumps({
                "body_part": "script",
                "main_module": "worker.js"
            })),
            'script': ('worker.js', script_content, 'application/javascript')
        }

        headers = {"Authorization": f"Bearer {self.api_token}"}

        try:
            response = requests.put(url, headers=headers, files=files, timeout=60)
            response.raise_for_status()
        except requests.RequestException as e:
            raise FlareProxError(f"Failed to create worker: {e}")

        worker_data = response.json()

        # Enable worker on subdomain - this is CRITICAL for subdomain to work
        # On freshly provisioned subdomains, this may need a retry
        subdomain_url = f"{self.base_url}/accounts/{self.account_id}/workers/scripts/{name}/subdomain"

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = requests.post(subdomain_url, headers=self.headers, json={"enabled": True}, timeout=30)
                if response.status_code in [200, 201]:
                    break  # Success!
                elif attempt < max_retries - 1:
                    # Wait before retry
                    time.sleep(5)
                else:
                    # Last attempt failed
                    print(f"  ⚠ Could not enable worker on subdomain (HTTP {response.status_code})")
                    error_data = response.json() if response.content else {}
                    if error_data.get("errors"):
                        print(f"     Error: {error_data['errors'][0].get('message', 'Unknown')}")
            except requests.RequestException as e:
                if attempt < max_retries - 1:
                    time.sleep(5)
                else:
                    print(f"  ⚠ Could not enable worker on subdomain: {e}")

        worker_url = f"https://{name}.{self.worker_subdomain}.workers.dev"

        return {
            "name": name,
            "url": worker_url,
            "created_at": time.strftime('%Y-%m-%d %H:%M:%S'),
            "id": worker_data.get("result", {}).get("id", name)
        }

    def list_deployments(self) -> List[Dict]:
        """List all FlareProx deployments."""
        url = f"{self.base_url}/accounts/{self.account_id}/workers/scripts"

        try:
            response = requests.get(url, headers=self.headers, timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            raise FlareProxError(f"Failed to list workers: {e}")

        data = response.json()
        workers = []

        for script in data.get("result", []):
            name = script.get("id", "")
            if name.startswith("flareprox-"):
                workers.append({
                    "name": name,
                    "url": f"https://{name}.{self.worker_subdomain}.workers.dev",
                    "created_at": script.get("created_on", "unknown")
                })

        return workers

    def wait_for_worker_ready(self, worker_url: str, worker_name: str, max_wait_seconds: int = 600) -> bool:
        """
        Wait for a worker to be fully provisioned and accessible.

        Args:
            worker_url: The full worker URL (e.g., https://worker.subdomain.workers.dev)
            worker_name: The worker name for logging
            max_wait_seconds: Maximum time to wait in seconds (default: 10 minutes)

        Returns:
            True if worker becomes ready, False if timeout
        """
        import sys

        start_time = time.time()
        attempt = 0
        check_interval = 2  # Check every 2 seconds

        spinner = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
        spinner_idx = 0

        while time.time() - start_time < max_wait_seconds:
            try:
                # Simple GET request to root to check if SSL/DNS is ready
                response = requests.get(worker_url, timeout=10, allow_redirects=False)
                # Any response (even 404 or 400) means the worker is accessible
                return True
            except requests.exceptions.SSLError:
                # SSL not ready yet, keep waiting
                pass
            except requests.exceptions.ConnectionError:
                # DNS/connection not ready yet, keep waiting
                pass
            except requests.RequestException:
                # Other errors, consider worker ready (SSL/DNS working)
                return True

            # Show spinner animation
            elapsed = int(time.time() - start_time)
            msg = f'\r     {spinner[spinner_idx % len(spinner)]} Waiting for worker to be ready... ({elapsed}s)'
            sys.stdout.write(msg)
            sys.stdout.flush()
            spinner_idx += 1

            time.sleep(0.5)
            attempt += 1

            # Check if we've exceeded max wait time
            if time.time() - start_time >= max_wait_seconds:
                sys.stdout.write('\r' + ' ' * 100 + '\r')  # Clear line
                sys.stdout.flush()
                return False

        sys.stdout.write('\r' + ' ' * 80 + '\r')  # Clear line
        sys.stdout.flush()
        return False

    def test_deployment(self, deployment_url: str, target_url: str, method: str = "GET") -> Dict:
        """Test a deployment endpoint."""
        test_url = f"{deployment_url}?url={target_url}"

        try:
            response = requests.request(method, test_url, timeout=30)
            return {
                "success": True,
                "status_code": response.status_code,
                "response_length": len(response.content),
                "headers": dict(response.headers)
            }
        except requests.RequestException as e:
            return {
                "success": False,
                "error": str(e)
            }

    def delete_workers(self, worker_names: List[str]) -> Dict[str, bool]:
        """
        Delete specific workers by name.

        Args:
            worker_names: List of worker names to delete

        Returns:
            Dictionary mapping worker names to success status
        """
        results = {}
        for name in worker_names:
            url = f"{self.base_url}/accounts/{self.account_id}/workers/scripts/{name}"
            try:
                response = requests.delete(url, headers=self.headers, timeout=30)
                if response.status_code in [200, 404]:
                    results[name] = True
                else:
                    results[name] = False
            except requests.RequestException:
                results[name] = False
        return results

    def cleanup_all(self) -> None:
        """Delete all FlareProx workers."""
        workers = self.list_deployments()

        if not workers:
            print(f"  • No workers to delete")
            return

        print(f"  Deleting {len(workers)} worker{'s' if len(workers) != 1 else ''}...\n")

        deleted_count = 0
        failed_count = 0

        for i, worker in enumerate(workers, 1):
            url = f"{self.base_url}/accounts/{self.account_id}/workers/scripts/{worker['name']}"
            try:
                response = requests.delete(url, headers=self.headers, timeout=30)
                if response.status_code in [200, 404]:
                    print(f"  ✓ [{i}/{len(workers)}] Deleted: {worker['name']}")
                    deleted_count += 1
                else:
                    print(f"  ✗ [{i}/{len(workers)}] Failed: {worker['name']}")
                    failed_count += 1
            except requests.RequestException as e:
                print(f"  ✗ [{i}/{len(workers)}] Error: {worker['name']}")
                failed_count += 1

        print(f"\n  Summary: {deleted_count} deleted, {failed_count} failed")


class FlareProx:
    """Main FlareProx manager class."""

    def __init__(self, config_file: Optional[str] = None):
        self.config = self._load_config(config_file)
        self.cloudflare = self._setup_cloudflare()
        self.endpoints_file = "flareprox_endpoints.json"
        self._ensure_config_file_exists()

    def _load_config(self, config_file: Optional[str] = None) -> Dict:
        """Load configuration from file."""
        config = {"cloudflare": {}}

        # Try specified config file
        if config_file and os.path.exists(config_file):
            config = self._load_config_file(config_file, config)

        # Try default config files
        default_configs = [
            "flareprox.json",
            "cloudproxy.json",  # Legacy support
            os.path.expanduser("~/.flareprox.json")
        ]

        for default_config in default_configs:
            if os.path.exists(default_config):
                config = self._load_config_file(default_config, config)
                break

        return config

    def _load_config_file(self, config_path: str, config: Dict) -> Dict:
        """Load configuration from a JSON file."""
        try:
            with open(config_path, 'r') as f:
                file_config = json.load(f)

            if "cloudflare" in file_config and not config["cloudflare"]:
                config["cloudflare"].update(file_config["cloudflare"])
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load config file {config_path}: {e}")

        return config

    def _setup_cloudflare(self) -> Optional[CloudflareManager]:
        """Setup Cloudflare manager if credentials are available."""
        cf_config = self.config.get("cloudflare", {})
        api_token = cf_config.get("api_token")
        account_id = cf_config.get("account_id")

        if api_token and account_id:
            return CloudflareManager(
                api_token=api_token,
                account_id=account_id,
                zone_id=cf_config.get("zone_id")
            )
        return None

    def _ensure_config_file_exists(self) -> None:
        """Create a default config file if none exists."""
        config_files = ["flareprox.json", os.path.expanduser("~/.flareprox.json")]

        # Check if any config file exists
        config_exists = any(os.path.exists(f) for f in config_files)

        if not config_exists:
            # Don't create a default config automatically
            # Let the user run 'python3 flareprox.py config' to set up
            pass

    @property
    def is_configured(self) -> bool:
        """Check if FlareProx is properly configured."""
        return self.cloudflare is not None

    def _save_endpoints(self, endpoints: List[Dict]) -> None:
        """Save endpoints to local file."""
        try:
            with open(self.endpoints_file, 'w') as f:
                json.dump(endpoints, f, indent=2)
        except IOError as e:
            print(f"Warning: Could not save endpoints: {e}")

    def _load_endpoints(self) -> List[Dict]:
        """Load endpoints from local file."""
        if os.path.exists(self.endpoints_file):
            try:
                with open(self.endpoints_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return []

    def sync_endpoints(self) -> List[Dict]:
        """Sync local endpoints with remote deployments."""
        if not self.cloudflare:
            return []

        try:
            endpoints = self.cloudflare.list_deployments()
            self._save_endpoints(endpoints)
            return endpoints
        except FlareProxError as e:
            print(f"Warning: Could not sync endpoints: {e}")
            return self._load_endpoints()

    def create_proxies(self, count: int = 1) -> Dict:
        """Create proxy endpoints."""
        if not self.cloudflare:
            raise FlareProxError("FlareProx not configured")

        print(f"\n{'=' * 70}")
        print(f"Creating {count} FlareProx endpoint{'s' if count != 1 else ''}...")
        print(f"{'=' * 70}")

        results = {"created": [], "failed": 0}

        # Step 1: Create all workers
        for i in range(count):
            try:
                endpoint = self.cloudflare.create_deployment()
                results["created"].append(endpoint)
                print(f"\n  ✓ Worker {i+1}/{count} created")
                print(f"    Name: {endpoint['name']}")
                print(f"    URL:  {endpoint['url']}")
            except FlareProxError as e:
                print(f"\n  ✗ Worker {i+1}/{count} failed: {e}")
                results["failed"] += 1

        # Step 2: Wait for workers to be provisioned
        if results["created"]:
            print(f"\n{'-' * 70}")
            print(f"Provisioning {len(results['created'])} worker{'s' if len(results['created']) != 1 else ''} - (can take 5+ mins on first run)")
            print(f"{'-' * 70}")

            provisioned = []
            for i, endpoint in enumerate(results["created"]):
                print(f"\n  [{i+1}/{len(results['created'])}] {endpoint['name']}")

                is_ready = self.cloudflare.wait_for_worker_ready(
                    endpoint['url'],
                    endpoint['name']
                )

                if is_ready:
                    print(f"     ✓ Ready!")
                    provisioned.append(endpoint)
                else:
                    print(f"     ✗ Timeout - worker may still be provisioning")
                    results["failed"] += 1

            # Update results to only include successfully provisioned workers
            results["created"] = provisioned

        # Update local cache
        self.sync_endpoints()

        # Final summary
        print(f"\n{'=' * 70}")
        total_created = len(results["created"])
        if total_created > 0:
            print(f"✓ Successfully created {total_created} worker{'s' if total_created != 1 else ''}")
            for endpoint in results["created"]:
                print(f"  • {endpoint['url']}")
        if results['failed'] > 0:
            print(f"✗ Failed: {results['failed']}")
        print(f"{'=' * 70}\n")

        return results

    def list_proxies(self) -> List[Dict]:
        """List all proxy endpoints."""
        endpoints = self.sync_endpoints()

        if not endpoints:
            print(f"\n{'=' * 70}")
            print(f"FlareProx Endpoints")
            print(f"{'=' * 70}")
            print("\n  No FlareProx endpoints found")
            print("  Create some with: python3 flareprox.py create\n")
            return []

        print(f"\n{'=' * 70}")
        print(f"FlareProx Endpoints ({len(endpoints)} total)")
        print(f"{'=' * 70}\n")

        for i, endpoint in enumerate(endpoints, 1):
            name = endpoint.get("name", "unknown")
            url = endpoint.get("url", "unknown")
            print(f"  {i}. {name}")
            print(f"     URL: {url}")
            print(f"     Status: Active\n")

        return endpoints


    def test_proxies(self, target_url: str = "https://ifconfig.me/ip", method: str = "GET") -> Dict:
        """Test proxy endpoints and show IP addresses."""
        endpoints = self._load_endpoints()

        if not endpoints:
            print(f"\n{'=' * 70}")
            print(f"Test FlareProx Endpoints")
            print(f"{'=' * 70}")
            print("\n  No proxy endpoints available. Create some first.\n")
            return {"success": False, "error": "No endpoints available"}

        results = {}
        successful = 0
        ip_to_workers = {}  # Track which workers have which IPs

        print(f"\n{'=' * 70}")
        print(f"Testing {len(endpoints)} FlareProx endpoint{'s' if len(endpoints) != 1 else ''}")
        print(f"{'=' * 70}")
        print(f"\n  Target URL: {target_url}")
        print(f"  Method: {method}\n")
        print(f"{'-' * 70}")

        for i, endpoint in enumerate(endpoints, 1):
            name = endpoint.get("name", "unknown")
            print(f"\n  [{i}/{len(endpoints)}] {name}")

            # Try multiple attempts with different delay
            max_retries = 2
            success = False
            result = None
            worker_ip = None

            for attempt in range(max_retries):
                try:
                    # Add small delay between retries
                    if attempt > 0:
                        time.sleep(1)
                        print(f"     Retry {attempt}...")

                    test_url = f"{endpoint['url']}?url={target_url}"
                    response = requests.request(method, test_url, timeout=30)

                    result = {
                        "success": response.status_code == 200,
                        "status_code": response.status_code,
                        "response_length": len(response.content),
                        "headers": dict(response.headers)
                    }

                    if response.status_code == 200:
                        success = True
                        print(f"     ✓ Request successful (Status: {result['status_code']})")

                        # Try to extract and show IP address from response
                        try:
                            response_text = response.text.strip()
                            if target_url in ["https://ifconfig.me/ip", "https://httpbin.org/ip"]:
                                if target_url == "https://httpbin.org/ip":
                                    # httpbin returns JSON
                                    data = response.json()
                                    if 'origin' in data:
                                        worker_ip = data['origin']
                                        print(f"       IP: {worker_ip}")
                                else:
                                    # ifconfig.me returns plain text IP
                                    if response_text and len(response_text) < 100:
                                        worker_ip = response_text
                                        print(f"       IP: {worker_ip}")
                                    else:
                                        print(f"       Response: {response_text[:100]}...")
                            else:
                                print(f"       Response Length: {result['response_length']} bytes")

                            # Track IP to worker mapping
                            if worker_ip:
                                if worker_ip not in ip_to_workers:
                                    ip_to_workers[worker_ip] = []
                                ip_to_workers[worker_ip].append(name)

                        except Exception as e:
                            print(f"       Response Length: {result['response_length']} bytes")

                        successful += 1
                        break  # Success, no need to retry

                    elif response.status_code == 503:
                        print(f"     ✗ Server unavailable (503) - target service may be overloaded")
                        if attempt < max_retries - 1:
                            continue  # Retry
                    else:
                        print(f"     ✗ Request failed (Status: {response.status_code})")
                        break  # Don't retry for other status codes

                except requests.RequestException as e:
                    if attempt < max_retries - 1:
                        print(f"     ✗ Connection error, retrying...")
                        continue
                    else:
                        print(f"     ✗ Request failed: {e}")
                        result = {"success": False, "error": str(e)}
                        break
                except Exception as e:
                    print(f"     ✗ Test failed: {e}")
                    result = {"success": False, "error": str(e)}
                    break

            results[name] = result if result else {"success": False, "error": "Unknown error"}

        # Display results
        unique_ips = set(ip_to_workers.keys())
        print(f"\n{'-' * 70}")
        print(f"Test Summary")
        print(f"{'-' * 70}\n")
        print(f"  ✓ Working: {successful}/{len(endpoints)}")
        if successful < len(endpoints):
            failed_count = len(endpoints) - successful
            print(f"  ✗ Failed: {failed_count} (may be due to target service issues)")
        if unique_ips:
            print(f"  • Unique IPs: {len(unique_ips)}")
            for ip in sorted(unique_ips):
                print(f"    - {ip}")

        # Check for duplicate IPs and offer cleanup
        duplicates_to_remove = []
        workers_to_keep = []

        for ip, workers in ip_to_workers.items():
            if len(workers) > 1:
                # Keep first, mark rest for removal
                workers_to_keep.append(workers[0])
                duplicates_to_remove.extend(workers[1:])

        if duplicates_to_remove:
            print(f"\n{'=' * 70}")
            print(f"Duplicate IPs - IPs can change, deletion is not always necessary")
            print(f"{'=' * 70}")
            print(f"\nFound {len(duplicates_to_remove)} worker(s) with duplicate IP addresses:")

            for ip, workers in ip_to_workers.items():
                if len(workers) > 1:
                    print(f"\n  IP: {ip}")
                    print(f"    Keep:   {workers[0]} (first)")
                    for dup in workers[1:]:
                        print(f"    Remove: {dup}")

            print(f"\n{len(set(ip_to_workers.keys()))} unique IP(s) would remain after cleanup.")

            # Prompt user
            try:
                choice = input(f"\nDelete {len(duplicates_to_remove)} duplicate worker(s)? (y/N): ").lower().strip()
                if choice == 'y':
                    print(f"\nDeleting {len(duplicates_to_remove)} worker(s)...")
                    delete_results = self.cloudflare.delete_workers(duplicates_to_remove)

                    deleted_count = sum(1 for success in delete_results.values() if success)
                    failed_count = len(duplicates_to_remove) - deleted_count

                    for name, success in delete_results.items():
                        if success:
                            print(f"  ✓ Deleted: {name}")
                        else:
                            print(f"  ✗ Failed:  {name}")

                    # Update local cache
                    self.sync_endpoints()

                    print(f"\n{'=' * 70}")
                    print(f"✓ Deleted {deleted_count} worker(s)")
                    if failed_count > 0:
                        print(f"✗ Failed to delete {failed_count} worker(s)")
                    print(f"{'=' * 70}\n")
                else:
                    print("Cleanup cancelled.")
            except KeyboardInterrupt:
                print("\n\nCleanup cancelled.")

        return results

    def cleanup_all(self) -> None:
        """Delete all proxy endpoints."""
        if not self.cloudflare:
            raise FlareProxError("FlareProx not configured")

        try:
            self.cloudflare.cleanup_all()
            print(f"\n  ✓ All endpoints deleted successfully")
        except FlareProxError as e:
            print(f"\n  ✗ Failed to cleanup: {e}")

        # Clear local cache
        if os.path.exists(self.endpoints_file):
            try:
                os.remove(self.endpoints_file)
                print(f"  ✓ Local cache cleared\n")
            except OSError:
                pass


def setup_interactive_config() -> bool:
    """Interactive setup for Cloudflare credentials."""
    print(f"\n{'=' * 70}")
    print(f"FlareProx Setup - Cloudflare Credentials")
    print(f"{'=' * 70}\n")
    print("  Getting Cloudflare Credentials:\n")
    print("  1. Sign up at https://cloudflare.com")
    print("  2. Go to https://dash.cloudflare.com/profile/api-tokens")
    print("  3. Click Create Token and use the 'Edit Cloudflare Workers' template")
    print("  4. Set the 'account resources' and 'zone resources' to all")
    print("     Click 'Continue to Summary'")
    print("  5. Click 'Create Token' and copy the token and your Account ID\n")
    print(f"{'-' * 70}\n")

    # Get API token
    api_token = getpass.getpass("  Enter your Cloudflare API token: ").strip()
    if not api_token:
        print("\n  ✗ API token is required\n")
        return False

    # Get account ID
    account_id = input("  Enter your Cloudflare Account ID: ").strip()
    if not account_id:
        print("\n  ✗ Account ID is required\n")
        return False

    # Create config
    config = {
        "cloudflare": {
            "api_token": api_token,
            "account_id": account_id
        }
    }

    # Save config file (overwrite if exists)
    config_path = "flareprox.json"
    try:
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
        print(f"\n{'=' * 70}")
        print(f"✓ Configuration Saved")
        print(f"{'=' * 70}\n")
        print(f"  Config file: {config_path}")
        print(f"  FlareProx is now configured and ready to use!\n")
        return True
    except IOError as e:
        print(f"\n  ✗ Error saving configuration: {e}\n")
        return False


def create_argument_parser() -> argparse.ArgumentParser:
    """Create and configure argument parser."""
    parser = argparse.ArgumentParser(description="FlareProx - Simple URL Redirection via Cloudflare Workers")

    parser.add_argument("command", nargs='?',
                       choices=["create", "list", "test", "cleanup", "help", "config", "serve", "serve-stop", "serve-status"],
                       help="Command to execute")

    parser.add_argument("--url", help="Target URL")
    parser.add_argument("--method", default="GET", help="HTTP method (default: GET)")
    parser.add_argument("--count", type=int, default=1, help="Number of proxies to create (default: 1)")
    parser.add_argument("--config", help="Configuration file path")
    parser.add_argument("--host", default="127.0.0.1", help="Local server host (serve)")
    parser.add_argument("--port", type=int, default=8080, help="Local server port (serve)")
    parser.add_argument("--daemon", action="store_true", help="Run server in background (serve)")
    parser.add_argument("--foreground", action="store_true", help="Internal flag to run server loop")
    parser.add_argument("--selection", choices=["random", "roundrobin"], default="random", help="Endpoint selection policy")
    parser.add_argument("--timeout", type=float, default=30.0, help="Upstream worker timeout seconds")

    return parser


def show_help_message() -> None:
    """Display the main help message."""
    print(f"\n{'=' * 70}")
    print(f"FlareProx - Simple URL Redirection via Cloudflare Workers")
    print(f"{'=' * 70}\n")
    print(f"  Usage: python3 flareprox.py <command> [options]\n")
    print(f"{'-' * 70}")
    print(f"Commands:")
    print(f"{'-' * 70}\n")
    print(f"  config    Show configuration help and setup")
    print(f"  create    Create new proxy endpoints")
    print(f"  list      List all proxy endpoints")
    print(f"  test      Test proxy endpoints and show IP addresses")
    print(f"  cleanup   Delete all proxy endpoints")
    print(f"  help      Show detailed help")
    print(f"  serve     Start local proxy server")
    print(f"  serve-stop Stop local proxy server")
    print(f"  serve-status Show server status\n")
    print(f"{'-' * 70}")
    print(f"Examples:")
    print(f"{'-' * 70}\n")
    print(f"  python3 flareprox.py config")
    print(f"  python3 flareprox.py create --count 2")
    print(f"  python3 flareprox.py test")
    print(f"  python3 flareprox.py test --url https://httpbin.org/ip\n")
    print(f"  python3 flareprox.py serve --host 127.0.0.1 --port 8080")
    print(f"  curl -s 'http://127.0.0.1:8080/?url=https://httpbin.org/ip'\n")

def _load_or_sync_endpoints(fp: "FlareProx") -> List[Dict]:
    endpoints = fp._load_endpoints()
    if not endpoints:
        endpoints = fp.sync_endpoints()
    return endpoints

def _choose_endpoint(endpoints: List[Dict], policy: str, state: Dict) -> Optional[str]:
    if not endpoints:
        return None
    if policy == "roundrobin":
        idx = state.get("idx", 0)
        url = endpoints[idx % len(endpoints)].get("url")
        state["idx"] = (idx + 1) % len(endpoints)
        return url
    return random.choice(endpoints).get("url")

def _make_handler(context: Dict):
    class ProxyRequestHandler(http.server.BaseHTTPRequestHandler):
        server_version = "FlareProxLocal/1.0"

        def log_message(self, format, *args):
            pass

        def _access_log(self, status: int, bytes_sent: int):
            remote = self.client_address[0] if self.client_address else "-"
            user = "-"
            ts = time.strftime('%d/%b/%Y:%H:%M:%S %z', time.localtime())
            req = self.requestline
            referer = self.headers.get("Referer", "-")
            ua = self.headers.get("User-Agent", "-")
            line = f"{remote} - {user} [{ts}] \"{req}\" {status} {bytes_sent} \"{referer}\" \"{ua}\""
            print(line, flush=True)

        def _forward(self):
            parsed = urlparse(self.path)
            target = None
            p = parsed.path
            if p.startswith("/http://") or p.startswith("/https://"):
                target = p[1:]
            if not target and parsed.scheme in ("http", "https") and parsed.netloc:
                target = self.path
            if not target:
                qs = parse_qs(parsed.query)
                u = qs.get("url", [None])[0]
                if u:
                    target = u
            if not target:
                hdr = self.headers.get("X-Target-URL")
                if hdr:
                    target = hdr

            if not target:
                body = json.dumps({"error": "No target URL specified"}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self._access_log(400, len(body))
                return

            worker = _choose_endpoint(context["endpoints"], context["policy"], context["state"])  # type: ignore
            if not worker:
                body = json.dumps({"error": "No worker endpoints available"}).encode()
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self._access_log(503, len(body))
                return

            upstream_url = f"{worker}?url={quote(target, safe='')}"

            exclude = {"host", "connection", "proxy-connection", "keep-alive", "transfer-encoding", "upgrade"}
            fwd_headers = {}
            for k, v in self.headers.items():
                if k.lower() in exclude:
                    continue
                fwd_headers[k] = v

            data = None
            if self.command not in ("GET", "HEAD"):
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length > 0:
                    data = self.rfile.read(length)

            try:
                resp = requests.request(self.command, upstream_url, headers=fwd_headers, data=data, timeout=context["timeout"])  # type: ignore
                content = resp.content
                self.send_response(resp.status_code)
                excluded_resp = {"transfer-encoding", "content-encoding"}
                for k, v in resp.headers.items():
                    if k.lower() in excluded_resp:
                        continue
                    if k.lower() == "content-length":
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                if content:
                    self.wfile.write(content)
                self._access_log(resp.status_code, len(content))
            except requests.RequestException as e:
                msg = json.dumps({"error": "Proxy request failed", "message": str(e)}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                self._access_log(502, len(msg))

        def do_GET(self):
            self._forward()

        def do_HEAD(self):
            self._forward()

        def do_POST(self):
            self._forward()

        def do_PUT(self):
            self._forward()

        def do_DELETE(self):
            self._forward()

        def do_OPTIONS(self):
            self._forward()

        def do_CONNECT(self):
            try:
                host, port_str = self.path.split(":", 1)
                port = int(port_str)
            except Exception:
                self.send_response(400)
                self.end_headers()
                self._access_log(400, 0)
                return

            try:
                upstream = socket.create_connection((host, port), timeout=10)
            except Exception:
                self.send_response(502)
                self.end_headers()
                self._access_log(502, 0)
                return

            try:
                self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                self.connection.setblocking(False)
                upstream.setblocking(False)
                sockets = [self.connection, upstream]
                bytes_out = 0
                while True:
                    rlist, _, _ = select.select(sockets, [], sockets, 30)
                    if not rlist:
                        break
                    for s in rlist:
                        try:
                            data = s.recv(8192)
                            if not data:
                                raise Exception("closed")
                            if s is self.connection:
                                upstream.sendall(data)
                            else:
                                self.connection.sendall(data)
                                bytes_out += len(data)
                        except Exception:
                            upstream.close()
                            self._access_log(200, bytes_out)
                            return
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass
            self._access_log(200, bytes_out)

    return ProxyRequestHandler

def run_local_proxy(fp: "FlareProx", host: str, port: int, selection: str, timeout: float):
    endpoints = _load_or_sync_endpoints(fp)
    context = {"endpoints": endpoints, "policy": selection, "state": {}, "timeout": timeout}
    handler = _make_handler(context)
    server = http.server.ThreadingHTTPServer((host, port), handler)
    try:
        print(f"\n  ✓ Local proxy server listening on http://{host}:{port}\n")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

def _pid_path() -> str:
    return os.path.join(os.getcwd(), "flareprox_server.pid")

def _write_pid(pid: int):
    with open(_pid_path(), "w") as f:
        f.write(str(pid))

def _read_pid() -> Optional[int]:
    p = _pid_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r") as f:
            return int(f.read().strip())
    except Exception:
        return None

def _remove_pid():
    p = _pid_path()
    try:
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def show_config_help() -> None:
    """Display configuration help and interactive setup."""
    print(f"\n{'=' * 70}")
    print(f"FlareProx Configuration")
    print(f"{'=' * 70}")

    # Check if already configured with valid credentials
    config_files = ["flareprox.json", os.path.expanduser("~/.flareprox.json")]
    valid_config_found = False
    existing_config_files = []

    for config_file in config_files:
        if os.path.exists(config_file):
            existing_config_files.append(config_file)
            try:
                with open(config_file, 'r') as f:
                    config_data = json.load(f)
                    cf_config = config_data.get("cloudflare", {})
                    api_token = cf_config.get("api_token", "").strip()
                    account_id = cf_config.get("account_id", "").strip()

                    # Check if we have actual credentials (not empty or placeholder)
                    if (api_token and account_id and
                        api_token not in ["", "your_cloudflare_api_token_here"] and
                        account_id not in ["", "your_cloudflare_account_id_here"] and
                        len(api_token) > 10 and len(account_id) > 10):
                        valid_config_found = True
                        break
            except (json.JSONDecodeError, IOError):
                continue

    if valid_config_found:
        print(f"\n  ✓ FlareProx is already configured with valid credentials.\n")
        print(f"  Configuration files found:")
        for config_file in existing_config_files:
            print(f"    - {config_file}")
        print()

        choice = input("  Do you want to reconfigure? (y/n): ").lower().strip()
        if choice != 'y':
            print()
            return

    elif existing_config_files:
        print(f"\n  Configuration files exist but appear to contain placeholder values:\n")
        for config_file in existing_config_files:
            print(f"    - {config_file}")
        print()

    print(f"  Setting up FlareProx configuration...\n")

    if setup_interactive_config():
        print(f"  You can now use FlareProx:")
        print(f"    python3 flareprox.py create --count 2")
        print(f"    python3 flareprox.py test\n")
    else:
        print(f"\n  ✗ Configuration failed. Please try again.\n")


def show_detailed_help() -> None:
    """Display detailed help information."""
    print(f"\n{'=' * 70}")
    print(f"FlareProx - Detailed Help")
    print(f"{'=' * 70}\n")
    print(f"  FlareProx provides simple URL redirection through Cloudflare Workers.")
    print(f"  All traffic sent to your FlareProx endpoints will be redirected to")
    print(f"  the target URL you specify, supporting all HTTP methods.\n")
    print(f"{'-' * 70}")
    print(f"Features:")
    print(f"{'-' * 70}\n")
    print(f"  • Support for all HTTP methods (GET, POST, PUT, DELETE, etc.)")
    print(f"  • Automatic CORS headers")
    print(f"  • IP masking through Cloudflare's global network")
    print(f"  • Simple URL-based redirection")
    print(f"  • Free tier: 100,000 requests/day")


def main():
    """Main entry point."""
    parser = create_argument_parser()
    args = parser.parse_args()

    # Show help if no command provided
    if not args.command:
        show_help_message()
        return

    if args.command == "config":
        show_config_help()
        return

    if args.command == "help":
        show_detailed_help()
        return

    # Initialize FlareProx
    try:
        flareprox = FlareProx(config_file=args.config)
    except Exception as e:
        print(f"\n  ✗ Configuration error: {e}\n")
        return

    if not flareprox.is_configured:
        print(f"\n{'=' * 70}")
        print(f"FlareProx Not Configured")
        print(f"{'=' * 70}\n")
        print(f"  Run 'python3 flareprox.py config' to set up FlareProx\n")
        return

    try:
        if args.command == "create":
            flareprox.create_proxies(args.count)

        elif args.command == "list":
            flareprox.list_proxies()

        elif args.command == "test":
            if args.url:
                flareprox.test_proxies(args.url, args.method)
            else:
                flareprox.test_proxies()  # Use default httpbin.org/ip

        elif args.command == "cleanup":
            print(f"\n{'=' * 70}")
            print(f"Cleanup All FlareProx Endpoints")
            print(f"{'=' * 70}\n")
            confirm = input("  Delete ALL FlareProx endpoints? (y/N): ")
            if confirm.lower() == 'y':
                flareprox.cleanup_all()
            else:
                print("\n  Cleanup cancelled.\n")
        elif args.command == "serve":
            if args.daemon and not args.foreground:
                cmd = [sys.executable, os.path.abspath(__file__), "serve", "--host", str(args.host), "--port", str(args.port), "--selection", args.selection, "--timeout", str(args.timeout), "--foreground"]
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _write_pid(proc.pid)
                print(f"\n  ✓ Server started in background (PID {proc.pid})\n")
                return
            run_local_proxy(flareprox, args.host, args.port, args.selection, args.timeout)
        elif args.command == "serve-stop":
            pid = _read_pid()
            if not pid:
                print("\n  ✗ No PID file found\n")
                return
            try:
                os.kill(pid, signal.SIGTERM)
                _remove_pid()
                print("\n  ✓ Server stopped\n")
            except Exception as e:
                print(f"\n  ✗ Failed to stop server: {e}\n")
        elif args.command == "serve-status":
            pid = _read_pid()
            if not pid:
                print("\n  • Server not running\n")
                return
            try:
                os.kill(pid, 0)
                print(f"\n  ✓ Server running (PID {pid})\n")
            except Exception:
                print("\n  • PID file exists but process not running\n")

    except FlareProxError as e:
        print(f"\n  ✗ Error: {e}\n")
    except KeyboardInterrupt:
        print("\n\n  Operation cancelled by user\n")
    except Exception as e:
        print(f"\n  ✗ Unexpected error: {e}\n")


if __name__ == "__main__":
    main()
