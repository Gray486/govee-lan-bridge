FROM python:3.12-alpine

# Standard library only: nothing is installed or downloaded at runtime.
WORKDIR /app
COPY bridge.py .

USER nobody
CMD ["python", "-u", "/app/bridge.py"]
