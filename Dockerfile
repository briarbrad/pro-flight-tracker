# Pro Flight Tracker — Railway Dockerfile
# Python 3.12 + Java 25 (required by the SWIM jumpstart JAR)
#
# IMPORTANT: swim/lib/jumpstart-jar-with-dependencies.jar is compiled to
# class file version 69 == Java 25. Java 17 or 21 will NOT run it; the JVM
# dies immediately with UnsupportedClassVersionError. Do not replace this
# with `apt-get install default-jdk` — no Debian release ships Java 25 yet.

FROM python:3.12-slim

# --- Java 25 ---------------------------------------------------------------
# Copied straight from the official Eclipse Temurin image. No download, no
# apt repo to go stale, and the version is pinned by the tag.
COPY --from=eclipse-temurin:25-jre /opt/java/openjdk /opt/java/openjdk
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

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
# NOTE: these are copied by name, so a new top-level module must be added here
# or it silently won't exist in the image and the app dies on import.
COPY app.py .
COPY store.py .
COPY scripts/ scripts/
COPY swim/ swim/
COPY references/ references/

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
