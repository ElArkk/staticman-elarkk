import os
import subprocess
import asyncio
import threading
import base64
import time
from typing import Dict

import modal
from fastapi import FastAPI, Request, Response
import httpx


app = modal.App("staticman-elarkk")


# Build a Python image with Node.js 18 and required Python libs.
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

image = (
    modal.Image.debian_slim()
    .apt_install("curl", "gnupg")
    .run_commands(
        "curl -fsSL https://deb.nodesource.com/setup_18.x | bash -",
        "apt-get install -y nodejs",
    )
    .pip_install("fastapi", "uvicorn", "httpx")
    .add_local_dir(REPO_ROOT, "/app", copy=True)
    .run_commands(
        "cd /app && (npm ci --omit=dev || npm install --production)"
    )
)


secret = modal.Secret.from_name("staticman-secrets")

# Global state
node_process = None
node_started = False
startup_error = None


def start_node_server():
    """Start the Node.js Staticman server."""
    global node_process, node_started, startup_error
    
    if node_started:
        return
    
    print("=== STARTING NODE.JS SERVER ===", flush=True)
    
    try:
        env = os.environ.copy()
        env.setdefault("NODE_ENV", "production")
        env["PORT"] = "8080"
        
        # Decode base64-encoded RSA key if present
        rsa_key_b64 = env.get('RSA_PRIVATE_KEY_B64', '')
        if rsa_key_b64:
            try:
                rsa_key = base64.b64decode(rsa_key_b64).decode('utf-8')
                env['RSA_PRIVATE_KEY'] = rsa_key
                print(f"Decoded RSA key from base64, length: {len(rsa_key)}", flush=True)
                print(f"RSA key starts with: {rsa_key[:50]}...", flush=True)
            except Exception as e:
                print(f"Failed to decode RSA key: {e}", flush=True)
                startup_error = f"RSA decode error: {e}"
                return
        
        print(f"Starting Node.js with NODE_ENV={env.get('NODE_ENV')}, PORT={env.get('PORT')}", flush=True)
        print(f"RSA_PRIVATE_KEY set: {bool(env.get('RSA_PRIVATE_KEY'))}", flush=True)
        print(f"GITHUB_TOKEN set: {bool(env.get('GITHUB_TOKEN'))}", flush=True)
        
        # Start Node.js
        node_process = subprocess.Popen(
            ["node", "/app/index.js"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        
        # Read output in background thread
        def read_output():
            if node_process.stdout:
                for line in node_process.stdout:
                    print(f"[node] {line.rstrip()}", flush=True)
        
        output_thread = threading.Thread(target=read_output, daemon=True)
        output_thread.start()
        
        # Wait for Node.js to start (blocking wait)
        for i in range(20):
            time.sleep(0.5)
            if node_process.poll() is not None:
                startup_error = f"Node.js exited with code {node_process.returncode}"
                print(f"=== {startup_error} ===", flush=True)
                return
            try:
                import urllib.request
                with urllib.request.urlopen("http://127.0.0.1:8080/", timeout=1) as resp:
                    if resp.status == 200:
                        print(f"Node.js ready after {(i+1)*0.5}s", flush=True)
                        node_started = True
                        return
            except Exception:
                pass
        
        startup_error = "Node.js did not become ready within 10s"
        print(f"=== {startup_error} ===", flush=True)
        
    except Exception as e:
        startup_error = str(e)
        print(f"=== STARTUP ERROR: {e} ===", flush=True)
        import traceback
        traceback.print_exc()


web = FastAPI()


def _sanitize_response_headers(headers: Dict[str, str]) -> Dict[str, str]:
    hop_by_hop = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
    return {k: v for k, v in headers.items() if k.lower() not in hop_by_hop}


@web.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(full_path: str, request: Request) -> Response:
    """Proxy incoming requests to the local Staticman Node.js server."""
    target_path = "/" + full_path
    target_url = f"http://127.0.0.1:8080{target_path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    content = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

    # Retry up to 5 times with backoff
    last_error = None
    for attempt in range(5):
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
                resp = await client.request(
                    request.method,
                    target_url,
                    content=content,
                    headers=headers,
                )
            clean_headers = _sanitize_response_headers(dict(resp.headers))
            return Response(content=resp.content, status_code=resp.status_code, headers=clean_headers)
        except httpx.RequestError as e:
            last_error = e
            await asyncio.sleep(0.5 * (attempt + 1))

    return Response(
        content=f"Staticman is starting up. Error: {last_error}".encode(),
        status_code=503,
        headers={"Retry-After": "2"}
    )


@app.function(image=image, secrets=[secret], timeout=60, memory=512)
@modal.asgi_app()
def fastapi_app():
    # Start Node.js server before returning the ASGI app
    start_node_server()
    return web


@app.function(image=image, secrets=[secret])
@modal.web_endpoint(method="GET", docs=True, requires_proxy_auth=True)
def debug():
    """
    Secured debug endpoint to check environment and Node.js status.

    Requires Modal Proxy Auth Token.
    Create a token at: https://modal.com/settings/proxy-auth-tokens

    Usage:
    curl -H "Modal-Key: <TOKEN_ID>" \\
         -H "Modal-Secret: <TOKEN_SECRET>" \\
         https://your-modal-url.modal.run/debug
    """
    # Start Node.js if not already started
    start_node_server()

    result = {
        "node_started": node_started,
        "startup_error": startup_error,
        "GITHUB_TOKEN_set": bool(os.environ.get("GITHUB_TOKEN")),
        "RSA_PRIVATE_KEY_B64_set": bool(os.environ.get("RSA_PRIVATE_KEY_B64")),
        "RSA_PRIVATE_KEY_B64_length": len(os.environ.get("RSA_PRIVATE_KEY_B64", "")),
        "node_process_exists": node_process is not None,
        "node_process_running": node_process is not None and node_process.poll() is None if node_process else False,
        "node_process_returncode": node_process.poll() if node_process else None,
    }

    # Try to decode RSA key
    rsa_b64 = os.environ.get("RSA_PRIVATE_KEY_B64", "")
    if rsa_b64:
        try:
            decoded = base64.b64decode(rsa_b64).decode("utf-8")
            result["rsa_key_decoded_length"] = len(decoded)
            result["rsa_key_starts_with"] = decoded[:50] if len(decoded) > 50 else decoded
        except Exception as e:
            result["rsa_decode_error"] = str(e)

    # Try to connect to Node.js
    try:
        import httpx
        with httpx.Client(timeout=2.0) as client:
            resp = client.get("http://127.0.0.1:8080/")
            result["node_health"] = resp.status_code
            result["node_response"] = resp.text[:200]
    except Exception as e:
        result["node_health_error"] = str(e)

    print(f"Debug result: {result}", flush=True)
    return result
