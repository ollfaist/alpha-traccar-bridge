FROM python:3.12-slim
WORKDIR /app
RUN pip install flask requests pillow
COPY app.py render_fastighet.py ./
COPY fastighet/*.gpkg fastighet/
CMD ["python", "app.py"]
