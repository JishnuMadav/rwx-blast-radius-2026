FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ADD --checksum=sha256:93b2445f15f395b81c20556777ece19a6c7507df950f13fa9fac4a3be739af0d https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sqlglot.tar.gz
RUN mkdir -p /opt/sqlglot && tar xzf /tmp/sqlglot.tar.gz -C /opt/sqlglot --strip-components=1 && rm /tmp/sqlglot.tar.gz
WORKDIR /app
COPY selector.py witness.py structure.py coverage.json.gz semantics.json.gz lifecycle.json.gz imports.json.gz ./
ENTRYPOINT ["python", "-B", "/app/selector.py"]
