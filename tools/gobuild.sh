#!/bin/sh
# Local build of the Go bot (the scorer uses the top-level Dockerfile on real amd64; Go's compiler crashes under QEMU here).
# Outputs .gobuild/mechbot-amd64 (sandbox test) and .gobuild/mechbot-mac (native test).
cd "$(dirname "$0")/.." || exit 1
docker run --rm --platform linux/arm64 -v "$PWD:/src" -w /src -e CGO_ENABLED=0 -e GOCACHE=/src/.gobuild/cache -e GOFLAGS=-buildvcs=false \
  golang:1.26.1-bookworm sh -c 'GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o .gobuild/mechbot-amd64 ./cmd/bot && GOOS=darwin GOARCH=arm64 go build -trimpath -o .gobuild/mechbot-mac ./cmd/bot' || exit 1
printf 'FROM scratch\nCOPY mechbot-amd64 /mechbot\nENTRYPOINT ["/mechbot"]\n' > .gobuild/Dockerfile
docker build -q --platform linux/amd64 -t mech-royale-go .gobuild >/dev/null && echo built
