FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY sync_sub2api.py provider_ranking.py web_app.py ./
COPY relaydesk ./relaydesk
EXPOSE 8787
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3).close()"
CMD ["python", "-B", "-u", "web_app.py", "--host", "0.0.0.0", "--port", "8787", "--data-dir", "/app/data"]
