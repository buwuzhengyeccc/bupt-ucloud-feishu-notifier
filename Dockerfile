FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
WORKDIR /app
COPY requirements.txt ./
COPY wheelhouse/ /tmp/wheels/
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels -r requirements.txt \
    && rm -rf /tmp/wheels \
    && groupadd --gid 10001 ucloud \
    && useradd --uid 10001 --gid 10001 --no-create-home ucloud
COPY *.py ./
USER 10001:10001
CMD ["python", "main.py"]
