FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN useradd --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
USER appuser

ENV BLUELINK_MCP_TRANSPORT=streamable-http BLUELINK_MCP_HOST=0.0.0.0 BLUELINK_MCP_PORT=8080
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ['BLUELINK_MCP_PORT']+'/mcp',timeout=3)" || exit 0
CMD ["python", "server.py"]
