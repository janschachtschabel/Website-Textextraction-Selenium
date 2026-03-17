# ======================================================================================
# Google Colab Deployment — Website Textextraction Selenium API
# Repository: https://github.com/janschachtschabel/Website-Textextraction-Selenium
#
# Colab resources: ~12 GB RAM, 2 vCPUs
# Configuration: Performance Setting — 2 workers, Selenium pool 2→4, no LLM (Presidio local)
# ======================================================================================

print("🚀 Website Textextraction Selenium — Google Colab Deployment")
print("=" * 60)

import functools
import os
import re
import subprocess
import sys
import threading
import time

import requests

# Set environment variables
# Performance Setting: 2 workers, pool 2→4, cache 30 min
# For low-resource runtimes use Safe Setting instead:
#   UVICORN_WORKERS=1, SELENIUM_POOL_SIZE=1, SELENIUM_MAX_POOL_SIZE=4,
#   RESULT_CACHE_TTL=300, RESULT_CACHE_MAX_SIZE=200
os.environ.update({
    "HOST":                      "0.0.0.0",
    "PORT":                      "8000",
    "LOG_LEVEL":                 "INFO",
    "DEFAULT_MODE":              "auto",
    "DEFAULT_JS_STRATEGY":       "speed",
    "DEFAULT_TIMEOUT_SECONDS":   "120",
    "DEFAULT_RETRIES":           "1",
    "DEFAULT_HEADLESS":          "true",
    "DEFAULT_STEALTH":           "true",
    "DEFAULT_JS_AUTO_WAIT":      "true",
    "DEFAULT_MAX_BYTES":         "10485760",
    # Performance Setting: 2 workers × 4 Chrome = 8 parallel JS renders
    # Load-test result: sweet-spot conc=4 at ~3 s latency, 0 errors
    "SELENIUM_POOL_SIZE":        "2",   # 4 Chrome at startup (2 per worker)
    "SELENIUM_MAX_POOL_SIZE":    "4",   # max. 8 Chrome (4 per worker)
    "SELENIUM_SCALE_THRESHOLD":  "0.8",
    "UVICORN_WORKERS":           "2",   # 2 workers; each warms its pool independently
    "MAX_QUEUE_SIZE":            "30",
    "QUEUE_TIMEOUT_SECONDS":     "90",
    "HTML_CONVERTER":            "trafilatura",
    "TRAFILATURA_CLEAN_MARKDOWN":"true",
    "RESULT_CACHE_TTL":          "1800", # 30 min – Colab content rarely changes
    "RESULT_CACHE_MAX_SIZE":     "500",  # 500 MB
    "ALLOW_INSECURE_SSL":        "false",
    "SSRF_PROTECTION":           "true",
    "GLOBAL_RATE_LIMIT_RPS":     "0",
    "DEFAULT_DOMAIN_RATE_LIMIT_RPS": "0",
    "MEDIA_CONVERSION_POLICY":   "skip",
})

# Install system packages
print("📦 Installing system packages...")
subprocess.run("sudo apt-get update -qq && sudo apt-get install -y git", shell=True, check=True)

# Install Chrome — skip if already present, otherwise try simple apt-get first
print("🌐 Installing Google Chrome...")
_chrome_present = subprocess.run(
    ["which", "google-chrome-stable"], capture_output=True
).returncode == 0 or subprocess.run(
    ["which", "google-chrome"], capture_output=True
).returncode == 0

if _chrome_present:
    print("✅ Google Chrome already installed — skipping")
else:
    # Simple apt-get install (works in most Colab runtimes)
    _r = subprocess.run(
        "sudo apt-get update -qq && sudo apt-get install -y google-chrome-stable",
        shell=True,
    )
    if _r.returncode != 0:
        # Fallback: add Google's signing key + repo manually
        print("   apt-get failed, adding Google repo manually...")
        subprocess.run(
            "curl -fsSL https://dl.google.com/linux/linux_signing_key.pub "
            "| sudo gpg --dearmor --yes "
            "-o /usr/share/keyrings/google-chrome-keyring.gpg && "
            "echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome-keyring.gpg] "
            "http://dl.google.com/linux/chrome/deb/ stable main' "
            "| sudo tee /etc/apt/sources.list.d/google-chrome.list > /dev/null && "
            "sudo apt-get update -qq && sudo apt-get install -y google-chrome-stable",
            shell=True, check=True,
        )
    print("✅ Google Chrome installed")

# Install Cloudflare Tunnel
print("🌐 Installing Cloudflare Tunnel...")
subprocess.run("wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb", shell=True, check=True)
subprocess.run("sudo dpkg -i cloudflared-linux-amd64.deb", shell=True, check=True)

# Clone repository
print("📥 Cloning repository...")
subprocess.run("git clone https://github.com/janschachtschabel/Website-Textextraction-Selenium.git /content/Website-Textextraction-Selenium", shell=True, check=True)
os.chdir('/content/Website-Textextraction-Selenium')
sys.path.insert(0, '/content/Website-Textextraction-Selenium')

# Install Python dependencies (explicit list — no pyproject.toml required)
print("📦 Installing Python dependencies...")
_PACKAGES = [
    "fastapi[standard]>=0.135.1",
    "uvicorn[standard]>=0.42.0",
    "httpx[http2]>=0.28.1",
    "selenium>=4.41.0",
    "webdriver-manager>=4.0.2",
    "markitdown[all]>=0.1.5",
    "trafilatura>=2.0.0",
    "beautifulsoup4>=4.14.3",
    "lxml>=5.3.0",
    "python-dotenv>=1.0.1",
    "tqdm>=4.66.0",
    "cachetools>=7.0.0",
    "loguru>=0.7.3",
    "truststore>=0.10.0",
    "aiolimiter>=1.1.0",
    "diskcache>=5.6.0",
    "presidio-analyzer>=2.2.0",
    "presidio-anonymizer>=2.2.0",
    "spacy>=3.7.0",
]
subprocess.run([sys.executable, "-m", "pip", "install", "-q"] + _PACKAGES, check=True)
print("✅ Python dependencies installed")

# Download spaCy models for Presidio anonymisation
print("🧠 Downloading spaCy models for PII anonymisation (one-time ~500 MB)...")
subprocess.run([sys.executable, "-m", "spacy", "download", "de_core_news_lg", "-q"], check=True)
subprocess.run([sys.executable, "-m", "spacy", "download", "en_core_web_lg", "-q"], check=True)
print("✅ spaCy models loaded")

# =============================================================================
# CHROME PATCH — set binary_location only
# All other flags (--no-sandbox, --disable-dev-shm-usage, etc.) are already
# included in js_fetcher._create_driver and must not be set again.
# =============================================================================

def _patch_chrome_for_colab():
    """Injects binary_location into every Options call made by js_fetcher."""
    try:
        from selenium.webdriver.chrome.options import Options
        _orig_init = Options.__init__

        @functools.wraps(_orig_init)
        def _patched_init(self, *args, **kwargs):
            _orig_init(self, *args, **kwargs)
            self.binary_location = "/usr/bin/google-chrome"

        Options.__init__ = _patched_init
        print("✅ Chrome binary_location set for Colab (/usr/bin/google-chrome)")
    except Exception as e:
        print(f"⚠️ Chrome patch failed: {e}")

os.environ["PYTHONPATH"] = "/content/Website-Textextraction-Selenium"
os.chdir("/content/Website-Textextraction-Selenium")

# =============================================================================
# HEALTH CHECK FUNCTIONS
# =============================================================================

def check_fastapi_health(port=8000, max_attempts=40):
    """Waits until /health reports status=ok (Selenium pool must be ready)."""
    for attempt in range(max_attempts):
        try:
            response = requests.get(f"http://localhost:{port}/health", timeout=3)
            if response.status_code == 200:
                data = response.json()
                if data.get("status") in ("ok", "starting"):
                    pools_ready = not data.get("pools_warming", True)
                    if pools_ready:
                        print(f"✅ API ready — Selenium pools initialised (attempt {attempt + 1})")
                        return True
                    else:
                        print(f"⏳ API running, Selenium pools still warming... (attempt {attempt + 1}/{max_attempts})")
                        time.sleep(3)
                        continue
        except requests.exceptions.RequestException:
            pass

        print(f"⏳ Waiting for API to start... (attempt {attempt + 1}/{max_attempts})")
        time.sleep(3)

    return False

def run_fastapi():
    """Starts FastAPI (2 workers, no --reload in Colab)."""
    print("🚀 Starting API server (Performance Setting: 2 workers, pool 2→4)...")
    os.chdir("/content/Website-Textextraction-Selenium")
    _patch_chrome_for_colab()

    process = subprocess.Popen(
        ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    for line in process.stdout:
        print(line.strip())
        if "Application startup complete" in line:
            print("✅ API server ready")
            break

    # Drain remaining pipe output in background to prevent buffer-full deadlock.
    # Without this, uvicorn workers block on stdout writes and stop serving requests.
    def _drain_stdout():
        try:
            for _ in process.stdout:
                pass
        except Exception:
            pass
    threading.Thread(target=_drain_stdout, daemon=True).start()

def start_cloudflare_tunnel(port):
    """Starts Cloudflare Tunnel and extracts the public URL."""
    print(f"🌐 Starting Cloudflare Tunnel for port {port}...")

    process = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    for line in process.stderr:
        print(f"Cloudflare: {line.strip()}")
        if "trycloudflare.com" in line:
            match = re.search(r'https?://[^\s]+', line)
            if match:
                return match.group(0)

    return None

# =============================================================================
# MAIN DEPLOYMENT SCRIPT
# =============================================================================

# Step 1: start FastAPI in a separate thread
print("🔧 Starting API server (Performance Setting: 2 workers, Selenium pool 2→4)...")
fastapi_thread = threading.Thread(target=run_fastapi)
fastapi_thread.daemon = True
fastapi_thread.start()

# Step 2: wait and health-check (Selenium pool needs ~15–30 s)
print("⏳ Waiting for API + Selenium pool...")
time.sleep(20)

if check_fastapi_health():
    print("✅ Website-Textextraction-Selenium API is running!")

    # Step 3: start Cloudflare Tunnel
    tunnel_url = start_cloudflare_tunnel(8000)

    if tunnel_url:
        print("\n🎉 API available at:")
        print(f"🌐 Public URL:      {tunnel_url}")
        print(f"📚 API Docs:        {tunnel_url}/docs")
        print(f"❤️  Health:          {tunnel_url}/health")
        print(f"📊 Stats:           {tunnel_url}/stats")
        print(f"🔗 Crawl Endpoint:  {tunnel_url}/crawl")

        print("\n📋 Example requests:")
        print(f"""
# Simple crawl (auto mode, with screenshot)
curl -X POST "{tunnel_url}/crawl" \\
  -H "Content-Type: application/json" \\
  -d '{{"url": "https://example.com", "mode": "auto", "screenshot": true}}'

# PII anonymisation (local, no API key)
curl -X POST "{tunnel_url}/crawl" \\
  -H "Content-Type: application/json" \\
  -d '{{"url": "https://example.com", "anonymize": true, "anonymize_language": "en"}}'

# Set per-domain rate limit
curl -X POST "{tunnel_url}/crawl" \\
  -H "Content-Type: application/json" \\
  -d '{{"url": "https://example.com", "crawl_rate_limit_rps": 0.5}}'
        """)

    else:
        print("❌ Cloudflare Tunnel could not be started")

else:
    print("❌ Website-Textextraction-Selenium API failed to start!")
    print("🔍 Debugging information:")

    # Directory check
    print("\n📁 Directory check:")
    subprocess.run(["ls", "-la", "/content/Website-Textextraction-Selenium/"], check=False)

    print("\n📁 App module check:")
    subprocess.run(["ls", "-la", "/content/Website-Textextraction-Selenium/app/"], check=False)

    # Dependencies check
    print("\n📦 Dependencies check:")
    subprocess.run([sys.executable, "-m", "pip", "show", "fastapi", "uvicorn", "selenium", "markitdown", "presidio-analyzer"], check=False)

    # App import test
    print("\n🔧 App import test:")
    subprocess.run([sys.executable, "-c", "from app.main import app; print('\u2705 App import successful')"], check=False, cwd="/content/Website-Textextraction-Selenium")

print("\n" + "=" * 60)
print("🏁 Deployment script finished")
