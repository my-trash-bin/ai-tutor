FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp IPYTHONDIR=/tmp/ipython OPENBLAS_NUM_THREADS=1
COPY requirements-worker.txt /opt/requirements-worker.txt
RUN pip install --no-cache-dir -r /opt/requirements-worker.txt
COPY worker.py /opt/worker.py
USER 65534:65534
WORKDIR /tmp
CMD ["python", "-u", "/opt/worker.py"]

