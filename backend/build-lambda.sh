#!/usr/bin/env bash
# Build the api Lambda deployment package.
#
# Produces backend/build/api/ - a flat directory of the `airhead` package plus its
# runtime dependencies, laid out the way Lambda expects on sys.path. Terraform zips
# it via `data "archive_file"` in infra/lambda.tf, so this script does not create
# the zip itself; run this, then `terraform apply`.
#
# The build directory is gitignored (`build/` and `*.zip`).
set -euo pipefail

cd "$(dirname "$0")"

BUILD_DIR="build/api"

# Must match `architectures` and `runtime` on aws_lambda_function.api. pydantic-core
# ships compiled wheels, so a mismatch here is not a warning at build time - it is an
# ImportError on the first request, from a .so built for the wrong architecture.
LAMBDA_ARCH="${LAMBDA_ARCH:-manylinux2014_aarch64}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"

echo "==> cleaning ${BUILD_DIR}"
# Full clean, not incremental. A stale dependency left behind by a removed pyproject
# entry ships to production and shadows nothing locally, so it is invisible until it
# is not.
rm -rf "${BUILD_DIR}" build/api.zip
mkdir -p "${BUILD_DIR}"

echo "==> resolving runtime dependencies from pyproject.toml"
# Read from pyproject rather than a parallel requirements.txt: two lists of
# dependencies drift, and tomllib is stdlib on 3.11+ so this costs no dependency.
mapfile -t DEPS < <(python3 - <<'PY'
import tomllib

with open("pyproject.toml", "rb") as fh:
    for dep in tomllib.load(fh)["project"]["dependencies"]:
        print(dep)
PY
)

if [ "${#DEPS[@]}" -eq 0 ]; then
  echo "no dependencies found in pyproject.toml - refusing to build" >&2
  exit 1
fi

echo "==> installing ${#DEPS[@]} dependencies for ${LAMBDA_ARCH} / py${PYTHON_VERSION}"
# --platform + --only-binary is what makes this cross-buildable: it downloads the
# wheels Lambda's runtime needs regardless of the machine running the build, instead
# of compiling for whatever this laptop happens to be. Without it, a build on x86
# macOS silently produces a package that cannot import on an arm64 Lambda.
python3 -m pip install \
  --target "${BUILD_DIR}" \
  --platform "${LAMBDA_ARCH}" \
  --python-version "${PYTHON_VERSION}" \
  --implementation cp \
  --only-binary=:all: \
  --upgrade \
  --quiet \
  "${DEPS[@]}"

echo "==> pruning the SDK the runtime already provides"
# boto3/botocore are ~70MB unzipped and are preinstalled in the Lambda Python
# runtime. Bundling them buys a newer SDK we do not need and pushes the artifact
# toward the 50MB zipped direct-upload limit, past which this needs an S3 staging
# bucket. If a future adapter ever needs an SDK feature newer than the runtime's,
# delete this block and add the S3 upload path - do not do both halfway.
rm -rf "${BUILD_DIR}"/boto3* "${BUILD_DIR}"/botocore* "${BUILD_DIR}"/s3transfer*

echo "==> copying the airhead package"
# Copied, not pip-installed: the package is pure Python, and `pip install .` cannot
# be combined with --platform/--only-binary=:all: (pip refuses to build a local
# source tree under those flags). Copying keeps one install invocation and one
# consistent set of wheels.
cp -r src/airhead "${BUILD_DIR}/airhead"

# Bytecode compiled for the build machine's interpreter is dead weight at best and
# a stale-import hazard at worst.
find "${BUILD_DIR}" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "${BUILD_DIR}" -type f -name '*.pyc' -delete

SIZE=$(du -sh "${BUILD_DIR}" | cut -f1)
echo "==> built ${BUILD_DIR} (${SIZE} unpacked)"
echo "    next: cd infra && terraform apply"
