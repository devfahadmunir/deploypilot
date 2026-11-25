FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd --create-home app
USER app
ENV MCP_TRANSPORT=http MCP_HOST=0.0.0.0
EXPOSE 8000
CMD ["python", "server/acme_server.py"]
