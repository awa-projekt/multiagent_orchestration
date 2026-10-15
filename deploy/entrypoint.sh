#!/bin/sh
# Adds the certificates in /etc/awa/ca/*.crt (your internal CA, or the CA of a TLS-inspecting
# proxy) to the public CA bundle of the image, and points every HTTP client awa uses at the
# result: httpx (model SDKs) and urllib read SSL_CERT_FILE, requests reads REQUESTS_CA_BUNDLE,
# the OTLP exporter of the Langfuse bridge reads OTEL_EXPORTER_OTLP_CERTIFICATE.
# Without extra certificates nothing changes. TLS verification is never switched off.
set -eu
bundle=/etc/ssl/certs/ca-certificates.crt
extra=$(find /etc/awa/ca -maxdepth 1 -name '*.crt' 2>/dev/null | sort)
if [ -n "$extra" ]; then
    {
        cat "$bundle"
        for f in $extra; do
            echo
            cat "$f"
        done
    } > /tmp/awa-ca-bundle.pem
    bundle=/tmp/awa-ca-bundle.pem
fi
export SSL_CERT_FILE="$bundle" REQUESTS_CA_BUNDLE="$bundle" OTEL_EXPORTER_OTLP_CERTIFICATE="$bundle"
exec "$@"
