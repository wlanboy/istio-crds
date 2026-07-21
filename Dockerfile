FROM registry.access.redhat.com/ubi10/python-314-minimal:latest

WORKDIR /opt/app-root/src

COPY requirements.txt ./
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

COPY istio.py kubectl.py istio-graph.py datenimport.py sync-job.py ./

ENTRYPOINT ["python3", "sync-job.py"]
