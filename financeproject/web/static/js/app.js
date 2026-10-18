/* financeproject: the interface's own script (no inline scripts: the Content-Security-Policy forbids them). */
"use strict";

(function () {
  const root = document.documentElement;
  const numberLocale = root.dataset.numberFormat === "1.234,56" ? "tr-TR" : "en-US";
  const textLocale = root.lang === "tr" ? "tr-TR" : "en-GB";

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  function number(value, decimals) {
    return new Intl.NumberFormat(numberLocale, { minimumFractionDigits: decimals, maximumFractionDigits: decimals }).format(value);
  }

  function compact(value) {
    return new Intl.NumberFormat(numberLocale, { notation: "compact", maximumFractionDigits: 1 }).format(value);
  }

  function parseDay(text) {
    const [y, m, d] = text.split("-").map(Number);
    return new Date(y, m - 1, d);
  }

  function monthLabel(text) {
    return new Intl.DateTimeFormat(textLocale, { month: "short", year: "2-digit" }).format(parseDay(text));
  }

  function longDate(text) {
    return new Intl.DateTimeFormat(textLocale, { day: "numeric", month: "long", year: "numeric" }).format(parseDay(text));
  }

  function tokens() {
    const style = getComputedStyle(root);
    const value = (name) => style.getPropertyValue(name).trim();
    return {
      accent: value("--accent"),
      grid: value("--grid"),
      baseline: value("--baseline"),
      muted: value("--muted"),
      ink: value("--ink"),
      ink2: value("--ink-2"),
      surface: value("--surface"),
    };
  }

  /* ---------------------------------------------------------- net worth chart */

  function networthChart() {
    const element = document.getElementById("networth-chart");
    const source = document.getElementById("networth-data");
    if (!element || !source || typeof window.echarts === "undefined") return;
    const data = JSON.parse(source.textContent);
    if (!data.dates.length) return;
    let unit = data.unit;
    const chart = window.echarts.init(element, null, { renderer: "svg" });

    function option() {
      const t = tokens();
      const values = data.units[unit];
      const decimals = data.decimals[unit];
      const suffix = data.suffixes[unit];
      const last = values.length - 1;
      return {
        animation: false,
        grid: { left: 4, right: 56, top: 14, bottom: 4, containLabel: true },
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "line", lineStyle: { color: t.baseline, width: 1 } },
          backgroundColor: t.surface,
          borderColor: t.grid,
          textStyle: { color: t.ink },
          extraCssText: "box-shadow: none; border-radius: 8px;",
          formatter(params) {
            const point = params[0];
            if (point.value === null || point.value === undefined) return "";
            const value = typeof point.value === "object" ? point.value.value : point.value;
            return (
              '<div class="tt-value">' + escapeHtml(number(value, decimals) + " " + suffix) + "</div>" +
              '<div class="tt-label">' + escapeHtml(longDate(point.axisValue)) + "</div>"
            );
          },
        },
        xAxis: {
          type: "category",
          boundaryGap: false,
          data: data.dates,
          axisLine: { lineStyle: { color: t.baseline } },
          axisTick: { show: false },
          axisLabel: { color: t.muted, formatter: monthLabel, hideOverlap: true },
        },
        yAxis: {
          type: "value",
          scale: false, // from zero: the shaded area stands for the whole amount
          splitNumber: 4,
          axisLabel: { color: t.muted, formatter: (v) => compact(v) },
          splitLine: { lineStyle: { color: t.grid, width: 1, type: "solid" } },
        },
        series: [
          {
            type: "line",
            name: data.labels[unit],
            data: values.map((v, i) =>
              i === last && v !== null
                ? { value: v, symbol: "circle", symbolSize: 8, itemStyle: { color: t.accent, borderColor: t.surface, borderWidth: 2 } }
                : v
            ),
            symbol: "none",
            showSymbol: true,
            connectNulls: false,
            lineStyle: { width: 2, color: t.accent, cap: "round", join: "round" },
            itemStyle: { color: t.accent },
            areaStyle: { color: t.accent, opacity: 0.1 },
            emphasis: { disabled: true },
            endLabel: { show: true, color: t.ink2, formatter: (p) => compact(typeof p.value === "object" ? p.value.value : p.value) },
          },
        ],
      };
    }

    function draw() {
      chart.setOption(option(), true);
      element.setAttribute("aria-label", data.summaries[unit]);
    }

    draw();
    document.querySelectorAll("[data-unit]").forEach((button) => {
      button.addEventListener("click", () => {
        unit = button.dataset.unit;
        document.querySelectorAll("[data-unit]").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
        document.querySelectorAll("[data-unit-table]").forEach((t) => t.classList.toggle("hidden", t.dataset.unitTable !== unit));
        draw();
      });
    });
    window.addEventListener("resize", () => chart.resize());
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
  }

  /* ---------------------------------------------------------- file drop zone */

  function dropzones() {
    document.querySelectorAll("[data-dropzone]").forEach((zone) => {
      const input = zone.querySelector('input[type="file"]');
      const name = zone.querySelector("[data-file-name]");
      if (!input) return;
      const show = () => {
        if (name) name.textContent = input.files && input.files.length ? input.files[0].name : "";
      };
      ["dragenter", "dragover"].forEach((type) =>
        zone.addEventListener(type, (event) => {
          event.preventDefault();
          zone.classList.add("over");
        })
      );
      ["dragleave", "drop"].forEach((type) =>
        zone.addEventListener(type, (event) => {
          event.preventDefault();
          zone.classList.remove("over");
        })
      );
      zone.addEventListener("drop", (event) => {
        if (event.dataTransfer && event.dataTransfer.files.length) {
          input.files = event.dataTransfer.files;
          show();
        }
      });
      input.addEventListener("change", show);
    });
  }

  /* ---------------------------------------------------------- confirmations */

  document.addEventListener(
    "submit",
    (event) => {
      const form = event.target;
      if (form instanceof HTMLFormElement && form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    },
    true
  );

  /* "Back" links go back in the history when there is one */
  document.addEventListener("click", (event) => {
    const link = event.target instanceof Element ? event.target.closest("[data-back]") : null;
    if (link && window.history.length > 1) {
      event.preventDefault();
      window.history.back();
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    networthChart();
    dropzones();
  });
})();
