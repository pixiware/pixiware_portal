#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
NODE="${NODE:-node}"

mkdir -p node_modules ../../static/dashboard-bg

install_pkg() {
  name=$1
  ver=$2
  if [ ! -d "node_modules/$name" ]; then
    curl -fsSL "https://registry.npmjs.org/${name}/-/${name}-${ver}.tgz" | tar -xz
    mv package "node_modules/$name"
  fi
}

install_pkg react 18.3.1
install_pkg react-dom 18.3.1
install_pkg ogl 1.0.11
install_pkg scheduler 0.23.2

if [ ! -x ".esbuild-bin/bin/esbuild" ]; then
  curl -fsSL "https://registry.npmjs.org/@esbuild/darwin-arm64/-/darwin-arm64-0.25.0.tgz" | tar -xz
  mv package .esbuild-bin
fi

.esbuild-bin/bin/esbuild --bundle src/main.jsx \
  --outfile=../../static/dashboard-bg/dashboard-bg.js \
  --format=esm \
  --jsx=automatic \
  --minify

cp src/Grainient.css ../../static/dashboard-bg/grainient.css
echo "Built static/dashboard-bg/dashboard-bg.js"
