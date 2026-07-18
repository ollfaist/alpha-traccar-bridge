FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
RUN pip install flask requests pillow
COPY app.py render_fastighet.py draw.html jakt.html jaktlag-setup.html jaktmap.html ./
COPY fastighet/*.gpkg fastighet/
CMD ["python", "app.py"]
