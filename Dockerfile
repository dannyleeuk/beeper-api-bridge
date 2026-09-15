FROM python:3.12-slim
RUN pip install --no-cache-dir pyyaml && useradd -r -u 10001 bridge && mkdir /data && chown bridge /data
WORKDIR /app
COPY beeper_api_bridge.py .
USER bridge
ENV BEEPER_API_BRIDGE_CONFIG=/data/config.json
# bind the api listener to the container interface: set "api_bind": "0.0.0.0" in config.json and publish 29338
# only on a private network. The appservice listener stays on loopback; run `bbctl proxy` in the same network namespace
# (see contrib/docker-compose.yml).
EXPOSE 29338
HEALTHCHECK CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:29338/healthz', timeout=3)" || exit 1
CMD ["python3", "/app/beeper_api_bridge.py"]
