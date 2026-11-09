#!/usr/bin/env bash
# Smoke test for the image. image-check passes the image tag as $1.
set -euo pipefail
image=$1

# The entrypoint is cbb-digest, so each check names its own. mjml-python ships a compiled extension;
# prove its wheel loads on the slim base, along with the package itself.
docker run --rm --entrypoint python "$image" -c "import mjml, cbb_digest.cli"

help=$(docker run --rm "$image" --help)
echo "$help"
for command in run serve send health; do
  if ! grep -qw "$command" <<<"$help"; then
    echo "::error title=smoke::cbb-digest --help doesn't list '$command'"
    exit 1
  fi
done

echo "smoke ok"
