# Cloud deployment of the auto-chope-seat scheduler.
# Build:  docker build -t auto-chope-seat .
# Run:    docker run -d -p 8000:8000 -e ACS_TOKEN=<long-random-secret> auto-chope-seat
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /srv/auto-chope-seat

# python3-tk satisfies app.py's tkinter import; the GUI itself is never started.
RUN apt-get update && apt-get install -y --no-install-recommends python3-tk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

COPY app.py server.py stations.json icon.png ./
COPY web ./web

ENV ACS_HEADLESS=1 PORT=8000
EXPOSE 8000

CMD ["python", "server.py"]
