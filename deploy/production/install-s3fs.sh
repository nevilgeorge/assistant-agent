#!/bin/bash
# AL2023: retain curl-minimal, pin the exact s3fs release and FUSE 2 ABI.
set -euo pipefail
s3fs_binary=${ASSISTANT_S3FS_BINARY:-/usr/local/bin/s3fs}
mode=${1:-}
if [[ $# -gt 2 || ( "$mode" != check && "$mode" != install ) ]]; then
  echo 'Usage: install-s3fs.sh check [binary] | install [staging-destination]' >&2
  exit 2
fi

valid_binary() {
  local binary=$1 architecture header libraries version
  [[ -f "$binary" && -x "$binary" ]] || return 1
  architecture=$(uname -m) || return 1
  header=$(LC_ALL=C readelf -h "$binary" 2>/dev/null) || return 1
  # Reject foreign executables before attempting to run them.
  case "$architecture" in
    x86_64) grep -Eq 'Machine:.*Advanced Micro Devices X86-64' <<< "$header" || return 1 ;;
    aarch64) grep -Eq 'Machine:.*AArch64' <<< "$header" || return 1 ;;
    *) echo "Unsupported s3fs host architecture: $architecture" >&2; return 1 ;;
  esac
  grep -Eq 'Class:.*ELF64' <<< "$header" || return 1
  libraries=$(LC_ALL=C ldd "$binary" 2>&1) || return 1
  [[ "$libraries" != *'not found'* ]] || return 1
  grep -q 'libfuse\.so\.2' <<< "$libraries" || return 1
  ! grep -q 'libfuse3\.so' <<< "$libraries" || return 1
  version=$("$binary" --version) || return 1
  grep -Eq '^.*V1\.95([^0-9.]|$)' <<< "${version%%$'\n'*}" || return 1
}

if [[ "$mode" == check ]]; then
  if valid_binary "${2:-$s3fs_binary}"; then exit 0; fi
  echo 'Expected native s3fs v1.95 with all runtime libraries and FUSE 2; rerun deployment with --setup-s3fs.' >&2
  exit 1
fi
# Explicit setup is idempotent, including when a staging destination is requested.
if valid_binary "$s3fs_binary"; then exit 0; fi
# Never use --allowerasing or install full curl. Preserve AL2023 minimal packages.
dnf_install=(dnf -y --setopt=protected_packages=dnf,systemd,systemd-udev,curl-minimal,libcurl-minimal install)
"${dnf_install[@]}" fuse fuse-libs gcc-c++ make automake autoconf libtool \
  binutils pkgconf-pkg-config fuse-devel libcurl-devel libxml2-devel openssl-devel
[[ $(pkg-config --modversion fuse) == 2.* ]] || { echo 'FUSE 2 development files missing' >&2; exit 1; }
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT
# Recorded from GitHub's v1.95 tag archive, 2026-10-12. Check before extracting.
curl --fail --location --retry 3 --connect-timeout 10 --max-time 180 \
  https://codeload.github.com/s3fs-fuse/s3fs-fuse/tar.gz/refs/tags/v1.95 -o "$build_dir/s3fs.tar.gz"
echo '0c97b8922f005500d36f72aee29a1345c94191f61d795e2a7b79fb7e3e6f5517  '"$build_dir/s3fs.tar.gz" | sha256sum --check --status
tar -xzf "$build_dir/s3fs.tar.gz" -C "$build_dir"
cd "$build_dir/s3fs-fuse-1.95"
./autogen.sh
./configure --prefix=/usr/local --with-openssl
# Limit memory use on the production host. Never install before candidate validation.
make -j2
valid_binary "$PWD/src/s3fs" || { echo 'Built s3fs failed architecture/version/runtime validation' >&2; exit 1; }
destination=${2:-$s3fs_binary}
install -D -m 0755 "$PWD/src/s3fs" "$destination"
valid_binary "$destination" || { echo 'Installed s3fs failed validation' >&2; exit 1; }
