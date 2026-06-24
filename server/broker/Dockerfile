FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV GRAPH_CACHE_DIR=/var/cache/icinga-ntfy/graphs
EXPOSE 8080

# 2 workers is plenty for action callbacks + image serving for a small team.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "2", "--access-logfile", "-", "app:app"]
