# syntax=docker/dockerfile:1

# Debian 13 (trixie). Chromium and its driver come from the distribution as a matched
# pair, which is what Selenium needs; a Chrome downloaded separately drifts out of step
# with its driver on the next rebuild and the browser then refuses to start.
FROM python:3.13-slim-trixie AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /src
# Only the lockfile: every version is pinned and every artefact hash-checked, so two
# builds of the same commit install the same bytes. The project itself is not built
# here - nothing reads its package metadata, so the source is copied in below and the
# build backend, which would be fetched unpinned and runs code, is never needed.
COPY requirements.lock ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock


FROM python:3.13-slim-trixie

# chromium-driver depends on chromium; both are named so the intent survives a rebuild.
# chromium-sandbox carries the SUID helper Debian splits out of the browser package. It is
# installed so an operator can choose Chrome's own sandbox; see SELENIUM_NO_SANDBOX below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium chromium-driver chromium-sandbox \
    && rm -rf /var/lib/apt/lists/*

# The service renders pages it does not trust, so it does not run as root.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 extraction

COPY --from=build /opt/venv /opt/venv
# python run.py puts /app on the import path, which is all the package needs.
COPY run.py /app/run.py
COPY app /app/app
COPY LICENSE /app/LICENSE
WORKDIR /app

# The result cache refuses to open unless it is private to the service user, so it is
# created here rather than left to a first run as root.
RUN install -d -o extraction -g extraction -m 0700 /var/cache/website-text-extraction

# SELENIUM_NO_SANDBOX: Docker's default seccomp profile blocks the namespaces Chrome's
# setuid sandbox needs, so the browser aborts with "Failed to move to new namespace". The
# container is the boundary instead - an unprivileged user, no new privileges, and the
# syscall filter left intact. To keep Chrome's own sandbox, set this to false and run with
# seccomp=unconfined, which trades the container's filter for it. See the README.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CHROME_BINARY=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    RESULT_CACHE_DIR=/var/cache/website-text-extraction \
    SELENIUM_NO_SANDBOX=true \
    HOST=0.0.0.0 \
    PORT=8000

# Reaching the container means binding beyond loopback, which the service refuses without
# an API key. Pass API_KEY at run time; there is deliberately no default.
EXPOSE 8000
USER extraction

# /health stays public even with a key configured, so the probe needs no credentials.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/health', timeout=4).status == 200 else 1)"]

CMD ["python", "run.py"]
