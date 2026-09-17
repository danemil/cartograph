#!/bin/sh
staging_dir() {
    echo "/var/tmp/staging"
}

prepare() {
    mkdir -p "$(staging_dir)"
}
