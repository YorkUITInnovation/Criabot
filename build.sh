#!/bin/bash

docker buildx build --push \
--platform linux/amd64,linux/arm64 \
--tag looponline/criabot:latest \
--tag looponline/criabot:v1.6.0 .