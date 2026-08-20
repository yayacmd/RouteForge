FROM python:3.12-slim

# OR-Tools needs a C++ runtime; curl is used by the container healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Data (config, saved plan, distance cache) lives here. Mount a volume so it
# survives container rebuilds.
ENV ROUTEFORGE_DATA=/data
RUN mkdir -p /data && useradd -r -u 10001 routeforge && chown -R routeforge /data /srv
USER routeforge

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
