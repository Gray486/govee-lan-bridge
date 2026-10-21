FROM python:3.12-alpine

# Standard library only: nothing is installed or downloaded at runtime.
WORKDIR /app
COPY bridge.py .

# devices.toml lives in /config. The bridge starts as root only to make that folder writable,
# then drops to an unprivileged user (nobody).
RUN mkdir /config
CMD ["python", "-u", "/app/bridge.py"]
