#!/usr/bin/env bash
# One-entry installation for Hearth and its local q38rocm Vulkan backend.
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
model_name=Qwen3.8-27B-Q3_K_M.gguf
model_target="$project_dir/data/q38rocm/models/$model_name"

usage() {
  cat <<EOF
Usage: bash deploy/install-hearth-q38rocm.sh [MODEL_FILE] [HEARTH_INSTALL_OPTIONS...]

Installs the q38rocm Vulkan build dependencies, prepares the model, builds and
installs its on-demand user service, then runs Hearth's standard first-time
installer. MODEL_FILE must be the $model_name file. If omitted, an existing
backend copy or a matching Hugging Face cache file is used.

HEARTH_INSTALL_OPTIONS are passed to deploy/install-first-time.sh, for example
--skip-browser or --no-service. The model weights are not downloaded by this
installer; supply the file or have it in the local Hugging Face cache.
EOF
}

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  usage
  exit 0
fi
if [[ $EUID -eq 0 ]]; then
  echo 'Run this installer as your regular user, without sudo.' >&2
  exit 1
fi
if [[ ! -f "$project_dir/deploy/install-first-time.sh" ]]; then
  echo 'Run this installer from a complete Hearth checkout.' >&2
  exit 1
fi

model_source=
hearth_options=()
if [[ $# -gt 0 && $1 != -* ]]; then
  model_source=$1
  shift
fi
hearth_options=("$@")

if [[ -z $model_source && -f $model_target ]]; then
  model_source=$model_target
fi
if [[ -z $model_source && -d "$HOME/.cache/huggingface/hub" ]]; then
  mapfile -t cached_models < <(find "$HOME/.cache/huggingface/hub" -type f -name "$model_name" -print 2>/dev/null)
  if [[ ${#cached_models[@]} -eq 1 ]]; then
    model_source=${cached_models[0]}
  elif [[ ${#cached_models[@]} -gt 1 ]]; then
    echo "Found multiple cached $model_name files; pass the desired file path explicitly." >&2
    printf '  %s\n' "${cached_models[@]}" >&2
    exit 1
  fi
fi
if [[ -z $model_source || ! -f $model_source ]]; then
  echo "Could not find $model_name. Pass its path as the first argument." >&2
  usage >&2
  exit 1
fi
if [[ $(basename -- "$model_source") != "$model_name" ]]; then
  echo "Expected a file named $model_name; got: $model_source" >&2
  exit 1
fi

mkdir -p "$(dirname -- "$model_target")"
if [[ $(realpath -- "$model_source") != $(realpath -m -- "$model_target") ]]; then
  if [[ -e $model_target ]]; then
    echo "Target already exists and differs from source: $model_target" >&2
    echo 'Move it aside or remove it, then rerun the installer.' >&2
    exit 1
  fi
  echo "Copying model into the q38rocm backend directory. This may take a while."
  cp --reflink=auto --sparse=always -- "$model_source" "$model_target"
fi

cd -- "$project_dir"
bash deploy/install-q38rocm-vulkan-deps.sh
bash deploy/build-q38rocm-vulkan.sh
bash deploy/install-q38rocm-service.sh
bash deploy/install-first-time.sh "${hearth_options[@]}"

cat <<EOF

Hearth and q38rocm installation steps completed.
The backend is installed for on-demand use and will not load the model at login.
Open Hearth's Models page and load $model_name to begin.
EOF
