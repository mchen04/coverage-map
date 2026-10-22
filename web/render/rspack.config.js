// Bundles the standalone organ-map render page with the OHIF workspace toolchain.
// Run by scripts/build_ohif.sh: RENDER_OUT=<dist>/render ORGAN_MAP_SRC=<organ-map/src/index.ts>
// OHIF_NODE_MODULES=<ohif-cov>/node_modules rspack build --config web/render/rspack.config.js
const path = require('path');

const need = name => {
  if (!process.env[name]) {
    throw new Error(`${name} is not set`);
  }
  return process.env[name];
};

module.exports = {
  mode: 'production',
  target: 'web',
  context: __dirname,
  entry: './main.ts',
  devtool: false,
  output: {
    path: path.resolve(need('RENDER_OUT')),
    filename: 'render.js',
    clean: true,
  },
  resolve: {
    extensions: ['.ts', '.js'],
    alias: { '@covmap/organ-map': path.resolve(need('ORGAN_MAP_SRC')) },
    modules: [path.resolve(need('OHIF_NODE_MODULES')), 'node_modules'],
  },
  resolveLoader: {
    modules: [path.resolve(need('OHIF_NODE_MODULES')), 'node_modules'],
  },
  module: {
    rules: [
      {
        test: /\.ts$/,
        loader: 'builtin:swc-loader',
        options: { jsc: { parser: { syntax: 'typescript' }, target: 'es2020' } },
        type: 'javascript/auto',
      },
      {
        test: /\.m?js$/,
        resolve: { fullySpecified: false },
      },
    ],
  },
  performance: { hints: false },
};
