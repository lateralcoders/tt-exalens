#!/bin/bash
# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

set -e

REPO=tenstorrent/tt-exalens
CI_IMAGE_NAME=ghcr.io/$REPO/tt-exalens-ci-ubuntu-22-04
IRD_IMAGE_NAME=ghcr.io/$REPO/tt-exalens-ird-ubuntu-22-04

# Compute the hash of the Dockerfile
DOCKER_TAG=$(./.github/get-docker-tag.sh)
echo "Docker tag: $DOCKER_TAG"

# Are we on main branch
ON_MAIN=$(git branch --show-current | grep -q main && echo "true" || echo "false")

build_and_push() {
    local image_name=$1 # Resulting image name
    local dockerfile=$2 # Dockerfile to build
    local on_main=$3 # Are we on main branch
    local from_image=$4 # Base image to build from

    if docker manifest inspect $image_name:$DOCKER_TAG > /dev/null; then
        echo "Image $image_name:$DOCKER_TAG already exists"
    else
        echo "Building image $image_name:$DOCKER_TAG"
        docker build \
            --progress=plain \
            --build-arg FROM_TAG=$DOCKER_TAG \
            ${from_image:+--build-arg FROM_IMAGE=$from_image} \
            -t $image_name:$DOCKER_TAG \
            -f $dockerfile .

        echo "Pushing image $image_name:$DOCKER_TAG"
        docker push $image_name:$DOCKER_TAG
    fi

    # If we are on main branch update manifest and add latest tag
    if [ "$on_main" = "true" ]; then
        echo "Adding latest tag to image $image_name:$DOCKER_TAG"
        docker manifest create $image_name:latest --amend $image_name:$DOCKER_TAG
        docker manifest push $image_name:latest
    fi
}

build_and_push $CI_IMAGE_NAME .github/Dockerfile.ci $ON_MAIN
build_and_push $IRD_IMAGE_NAME .github/Dockerfile.ird $ON_MAIN ci

echo "All images built and pushed successfully"
echo "CI_IMAGE_NAME:"
echo $CI_IMAGE_NAME:$DOCKER_TAG
