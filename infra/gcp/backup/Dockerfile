# Nightly backup job: pg_dump -> Cloud Storage. Built FROM the exact Postgres
# image the database runs, so pg_dump's version always matches the server and
# the image shares its layers in Artifact Registry (almost no extra storage).
ARG PG_IMAGE=postgres:17-alpine
FROM ${PG_IMAGE}
RUN apk add --no-cache curl
COPY --chmod=755 backup.sh /backup.sh
USER postgres
ENTRYPOINT ["/backup.sh"]
