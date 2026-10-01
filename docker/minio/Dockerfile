# MinIO no longer publishes the image used by the upstream Compose file.
# Build a fixed upstream source release instead of depending on a third-party image.
FROM golang:1.24.8-bookworm AS build
ENV CGO_ENABLED=0
RUN go install -trimpath github.com/minio/minio@RELEASE.2025-10-15T17-29-55Z

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /go/bin/minio /usr/local/bin/minio
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
