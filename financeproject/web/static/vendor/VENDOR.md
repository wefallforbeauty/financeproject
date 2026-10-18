# Bundled libraries

The interface loads nothing from the internet. These two libraries are
bundled unchanged, as published on npm (the package's integrity hash was
checked when they were taken):

| File | Package | Version | License | SHA-256 |
|---|---|---|---|---|
| `htmx.min.js` | [htmx.org](https://htmx.org) | 2.0.11 | 0BSD (`htmx.LICENSE`) | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |
| `echarts.min.js` | [Apache ECharts](https://echarts.apache.org) | 6.1.0 | Apache-2.0 (`echarts.LICENSE`, `echarts.NOTICE`) | `b66b25aeb4df84e33199dc21694014d336d222cbd9deb0e5a7c14bd6aa0d0fd0` |

To update one: download the package from the npm registry, check its
`dist.integrity`, copy the file and its license here, and update this table.
