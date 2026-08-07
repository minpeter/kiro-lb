# kiro-lb - Docker Image
# Optimized single-stage build

FROM python:3.10-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache

# Create non-root user for security
RUN groupadd -r kiro && useradd -r -g kiro kiro

# Set working directory and give ownership to kiro user
WORKDIR /app
RUN chown kiro:kiro /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the tiktoken vocabularies into the image. tiktoken ships no BPE data: it
# downloads it from openaipublic.blob.core.windows.net on first use and caches it
# under a temp dir, so without this every fresh container pays a ~5.3MB download
# on its first token count, and a network-restricted deployment silently falls
# back to character-based estimation instead of counting.
RUN mkdir -p /opt/tiktoken-cache \
    && python -c "import tiktoken; [tiktoken.get_encoding(n) for n in ('cl100k_base', 'o200k_base')]" \
    && chmod -R a+rX /opt/tiktoken-cache

# Copy application code
COPY --chown=kiro:kiro . .

# Remove runtime files that should not be in image
# (in case they were copied from build context or cache)
RUN rm -f credentials.json state.json

# Create writable runtime directories with proper permissions.
RUN mkdir -p debug_logs data && chown -R kiro:kiro debug_logs data

# Switch to non-root user
USER kiro

# Expose port
EXPOSE 8000

# Health check
# Using httpx (our main HTTP library) instead of requests
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health', timeout=5)"

# Run the application
CMD ["python", "main.py"]
