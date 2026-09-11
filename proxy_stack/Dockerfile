# Pinned by digest: this proxy's job is protocol fidelity, so the interpreter
# underneath it must not move on a rebuild.
FROM python:3.13-alpine@sha256:46ee549c88617e9bc8acb843a326f1a5c0fa5608d7f9703509efe6d53b55f318

WORKDIR /app
COPY proxy.py /app/

USER 1000:1000

EXPOSE 8111
CMD ["python", "-u", "/app/proxy.py"]
