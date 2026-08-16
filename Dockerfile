# Pro Flight Tracker — Railway Dockerfile
# Python 3.12 + Java 21 (for SWIM JMS client)
#
# We use Python 3.12 (widely supported) and Java 21 LTS.
# The jumpstart JAR was compiled with Java 25, so we install
# the latest OpenJDK available in the base image and fall back
# to downloading if needed.

FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install OpenJDK 21+ (required for SWIM jumpstart JAR)
# The JAR needs Java 25 class files — install latest available
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-jdk \
    && rm -rf /var/lib/apt/lists/* \
    || ( \
    # Fallback: download Eclipse Temurin JDK 21
    curl -sL "https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jdk/hotspot/normal/eclipse" -o /tmp/jdk.tar.gz && \
    mkdir -p /usr/lib/jvm && \
    tar -xzf /tmp/jdk.tar.gz -C /usr/lib/jvm && \
    rm /tmp/jdk.tar.gz && \
    ln -s /usr/lib/jvm/jdk-* /usr/lib/jvm/default && \
    update-alternatives --install /usr/bin/java java /usr/lib/jvm/default/bin/java 1 \
    )

# Verify Java
RUN java -version 2>&1 || echo "WARNING: Java not found — SWIM feeds will not work"

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .
COPY scripts/ scripts/
COPY swim/ swim/
COPY references/ references/

# Make swim/bin/run executable
RUN chmod +x swim/bin/run 2>/dev/null || true

# Expose port (Railway sets PORT env var)
EXPOSE 8080

# Start with gunicorn for production
CMD gunicorn --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 120 app:app
