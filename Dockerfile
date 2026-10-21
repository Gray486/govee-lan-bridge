FROM python:3.12-alpine

# Standard library only: nothing is installed or downloaded at runtime.
WORKDIR /app
COPY bridge.py .

# devices.toml lives in /config; mount a host folder there and run as its owner (see README).
RUN mkdir /config && chown nobody /config
USER nobody
CMD ["python", "-u", "/app/bridge.py"]
