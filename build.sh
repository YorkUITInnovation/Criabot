#!/bin/bash

docker buildx build --push \
--platform linux/amd64,linux/arm64 \
--tag uitadmin/criabot:latest-beta \
--tag uitadmin/criabot:v1.8.2-beta .