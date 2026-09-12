# Pro Flight Tracker — Railway Dockerfile
# Python 3.12 + Java 25 (required by the SWIM jumpstart JAR)
#
# IMPORTANT: swim/lib/jumpstart-jar-with-dependencies.jar is compiled to
# class file version 69 == Java 25. Java 17 or 21 will NOT run it; the JVM
# dies immediately with UnsupportedClassVersionError. Do not replace this
# with `apt-get install default-jdk` — no Debian release ships Java 25 yet.

# --- Base ------------------------------------------------------------------
FROM python:3.12-slim AS base

# --- Java 25 ---------------------------------------------------------------
# Copied straight from the official Eclipse Temurin image, pinned by digest
# (verified 2026-09-12 via registry-1.docker.io). No download, no apt repo
# to go stale. Bump the digest deliberately, never by floating the tag.
COPY --from=eclipse-temurin:25-jre@sha256:15090d159279e5c158473eccb48cd87f57b3e3a47511a797eb5a7a7ea6f86b0f \
    /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    dos2unix \
    && rm -rf /var/lib/apt/lists/*

# Hard gate: fail the BUILD (not the first request) if Java is too old.
RUN set -eux; \
    java -version; \
    ver="$(java -XshowSettings:properties -version 2>&1 \
           | awk -F'= *' '/java.specification.version/{print $2}')"; \
    echo "Detected Java specification version: ${ver}"; \
    [ "${ver}" -ge 25 ] || { \
      echo "FATAL: SWIM jumpstart JAR requires Java 25+, found ${ver}"; exit 1; }

# Set working directory
WORKDIR /app

# Install Python dependencies (pinned; see requirements.lock)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code. COPY . . plus .dockerignore — never a named-module
# list: a new top-level package (like pft/) must not silently miss the image.
COPY . .

# --- Tests (blocking) ------------------------------------------------------
# The full pytest suite runs at build time. The production stage COPY --from
# this stage, which forces Docker to build it — a red suite fails the deploy,
# not the first request.
FROM base AS test
RUN pip install --no-cache-dir -r requirements-test.txt \
    && python -m pytest tests/ -q \
    && touch /tmp/tests-passed

# --- Production ------------------------------------------------------------
FROM base AS prod
COPY --from=test /tmp/tests-passed /tmp/tests-passed

# Normalize line endings and make the launcher executable
RUN dos2unix swim/bin/run && chmod +x swim/bin/run

# Smoke test: the JAR's main class must at least load under this JVM.
# It exits non-zero for missing config, which is fine — we only care that
# it is NOT an UnsupportedClassVersionError.
RUN set -eux; \
    out="$(java -jar swim/lib/jumpstart-jar-with-dependencies.jar 2>&1 || true)"; \
    echo "${out}" | head -5; \
    case "${out}" in \
      *UnsupportedClassVersionError*) echo "FATAL: JVM cannot load the SWIM JAR"; exit 1 ;; \
    esac

# Expose port (Railway sets PORT env var)
EXPOSE 8080

# Start with gunicorn for production
CMD gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 120 app:app
