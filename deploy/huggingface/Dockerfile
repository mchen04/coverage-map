FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends orthanc orthanc-dicomweb curl \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir numpy==2.5.4 aiohttp==3.14.5

RUN useradd -m -u 1000 user
USER user
WORKDIR /home/user/app
COPY --chown=user . .

ENV COVMAP_DATA=/home/user/app/data \
    COVMAP_OHIF_DIST=/home/user/app/ohif-dist \
    COVMAP_ORTHANC_DB=/tmp/orthanc-db \
    PYTHONUNBUFFERED=1
EXPOSE 7860
CMD ["bash", "start.sh"]
