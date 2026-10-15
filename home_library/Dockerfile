FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LIBRARY_DATA_DIR=/data

WORKDIR /opt/home_library
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY run.py .
COPY app ./app

VOLUME /data
EXPOSE 8099
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "run.py"]
