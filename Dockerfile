FROM python:3.13-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY covermap ./covermap
COPY server ./server
COPY webapp ./webapp
COPY data ./data
COPY main.py ./
# Жалобы, замеры и кеш объявлений пишутся в /srv/var (том). Создаём заранее.
RUN mkdir -p /srv/var
EXPOSE 8000
# proxy_headers=False: IP берётся из соединения. За Caddy это IP Caddy — см. DEPLOY.md.
CMD ["python", "-m", "server", "--host", "0.0.0.0", "--port", "8000"]
