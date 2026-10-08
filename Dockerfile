FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts ./scripts
RUN sh scripts/get_fonts.sh /srv/fonts
COPY app ./app
ENV DATA_DIR=/srv/data PORT=8000 FONTS_DIR=/srv/fonts
VOLUME ["/srv/data"]
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
