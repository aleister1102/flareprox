## Overview
- Implement a lightweight Python HTTP server that forwards incoming requests to Cloudflare Worker endpoints managed by FlareProx.
- Integrate a `serve` command into `flareprox.py` to start the server (foreground or background), plus `serve-stop` and `serve-status` for lifecycle management.
- Use only standard library + `requests` to match existing dependencies.

## Server Design
- Use `http.server` with `ThreadingHTTPServer` for concurrency.
- Implement `ProxyRequestHandler` to:
  - Derive target from one of: request path `/<http(s)://...>`, query `?url=...`, or header `X-Target-URL` (aligned with Worker: `flareprox.py:237-253`).
  - Choose a Cloudflare worker endpoint at request time from `flareprox_endpoints.json` (random or round-robin).
  - Construct worker URL as `"{worker_base}/" + quote(target_url)` when path-based, or `"{worker_base}?url=" + quote(target_url)` for query-based.
  - Forward method, headers (minus hop-by-hop like `Host`), and body using `requests`; set reasonable timeouts.
  - Stream response back to the client with status and headers from the Worker. The Worker already injects CORS and strips problematic headers (`flareprox.py:286-314`).
- Handle errors with JSON responses and appropriate status codes (400 for missing/invalid target, 502/504 for upstream issues).

## Endpoint Management
- Load endpoints from `flareprox_endpoints.json` created by FlareProx create/sync (`flareprox.py:610-639`).
- If empty, print an error advising `create` first, or optionally trigger a sync before starting.
- Selection policy: default random per request; simple round-robin optional via a flag.

## CLI Integration
- Extend `argparse` in `flareprox.py` to add commands:
  - `serve` to start the local proxy; flags: `--host` (default `127.0.0.1`), `--port` (default `8080`), `--daemon` (background), `--selection` (`random|roundrobin`).
  - `serve-stop` to stop a background server using a PID file `flareprox_server.pid` in project root.
  - `serve-status` to report if the server is running by checking the PID file and process existence.
- For `--daemon`:
  - Spawn a detached child using `subprocess.Popen([sys.executable, "flareprox.py", "serve", "--host", ..., "--port", ..., "--foreground"])` or an internal `--server-mode` flag to run the server loop.
  - Write PID to `flareprox_server.pid` and return control to the CLI.

## Request Mapping Examples
- Path-based: `GET /https://example.com/path?x=1` → `GET "https://<worker>.../https%3A%2F%2Fexample.com%2Fpath?x=1"`.
- Query-based: `GET /?url=https://httpbin.org/ip` → `GET "https://<worker>...?url=https%3A%2F%2Fhttpbin.org%2Fip"`.
- Header-based: `X-Target-URL: https://example.com` → `GET "https://<worker>...?url=..."` or path form.

## Behavior Notes
- Preserve client headers to the Worker so it can forward allowed ones to the target (`flareprox.py:255-283`). Remove `Host` before sending to Worker.
- Methods: forward all common methods; body forwarded for non-`GET`/`HEAD`.
- Timeouts: request timeout ~30s; connection timeout ~10s; configurable via flags.
- Logging: minimal request/response status to stdout; no secret logging.

## Background Invocation
- FlareProx `serve --daemon` starts the server in background and immediately exits with the PID.
- `serve-stop` reads PID from `flareprox_server.pid` and terminates the process (best-effort cross-platform using `os.kill`).
- `serve-status` reports running/stopped state.

## Validation Plan
- Precondition: run `create --count 1` to ensure at least one Worker endpoint is available; confirm via `list`.
- Start server: `python3 flareprox.py serve --host 127.0.0.1 --port 8080 --daemon`.
- Test path-based: `curl -s http://127.0.0.1:8080/https://ifconfig.me/ip` → expect an IP.
- Test query-based: `curl -s 'http://127.0.0.1:8080/?url=https://httpbin.org/ip'` → expect JSON with `origin`.
- Load test small concurrency: run 5 parallel curls to verify `ThreadingHTTPServer` handles multiple requests.
- Stop server: `python3 flareprox.py serve-stop` and verify `serve-status` shows stopped.

## Files & Conventions
- Implement server within `flareprox.py` to avoid new files and keep CLI unified.
- Follow existing style: plain functions/classes, `requests`, no external deps, and no code comments.
- Respect existing config and cache files: `flareprox.json`, `flareprox_endpoints.json`.

## Rollout
- Implement new server classes and CLI commands.
- Verify locally with the validation steps.
- Provide usage examples in the CLI help output for `serve`, `serve-stop`, `serve-status`. 