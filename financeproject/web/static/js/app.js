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

  /* 23.1% in English, %23,1 in Turkish (the number in the chosen number format) */
  function pct(fraction, decimals) {
    const text = number(fraction * 100, decimals);
    return root.lang === "tr" ? "%" + text : text + "%";
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
      series2: value("--series-2"),
      series3: value("--series-3"),
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

  /* ---------------------------------------------------------- charts of the reports */

  /* A chart's numbers: in a data-json attribute (parts of a page that htmx replaces, which leaves out
     <script> elements), or in the <script type="application/json"> that data-source names. */
  function chartData(element) {
    if (element.dataset.json) return JSON.parse(element.dataset.json);
    const source = document.getElementById(element.dataset.source || "");
    return source ? JSON.parse(source.textContent) : null;
  }

  function redrawOnThemeChange(draw, chart) {
    draw();
    window.addEventListener("resize", () => chart.resize());
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
  }

  /* Grouped bars, one series per entry of data.series (two at most here: income and expenses). */
  function barsChart(element) {
    const data = chartData(element);
    if (!data || !data.categories.length) return;
    const chart = window.echarts.init(element, null, { renderer: "svg" });
    const last = data.categories.length - 1;

    function draw() {
      const t = tokens();
      const colors = [t.accent, t.series2, t.series3];
      chart.setOption(
        {
          animation: false,
          color: colors,
          grid: { left: 4, right: 8, top: 36, bottom: 4, containLabel: true },
          legend: { top: 0, left: 0, itemWidth: 12, itemHeight: 12, icon: "roundRect", textStyle: { color: t.ink2 } },
          tooltip: {
            trigger: "item",
            backgroundColor: t.surface,
            borderColor: t.grid,
            textStyle: { color: t.ink },
            extraCssText: "box-shadow: none; border-radius: 8px;",
            formatter(p) {
              return (
                '<div class="tt-value">' + escapeHtml(number(p.value, data.decimals) + " " + data.suffix) + "</div>" +
                '<div class="tt-label">' + escapeHtml(p.seriesName + " · " + monthLabel(p.name + "-01")) + "</div>"
              );
            },
          },
          xAxis: {
            type: "category",
            data: data.categories,
            axisLine: { lineStyle: { color: t.baseline } },
            axisTick: { show: false },
            axisLabel: { color: t.muted, formatter: (v) => monthLabel(v + "-01") },
          },
          yAxis: {
            type: "value",
            splitNumber: 4,
            axisLabel: { color: t.muted, formatter: (v) => compact(v) },
            splitLine: { lineStyle: { color: t.grid } },
          },
          series: data.series.map((s) => ({
            type: "bar",
            name: s.name,
            barGap: "6%",
            barCategoryGap: "38%",
            itemStyle: { borderRadius: [4, 4, 0, 0], borderColor: t.surface, borderWidth: 1 },
            emphasis: { disabled: true },
            data: s.values.map((v, i) =>
              i === last ? { value: v, label: { show: true, position: "top", color: t.ink2, formatter: (p) => compact(p.value) } } : v
            ),
          })),
        },
        true
      );
      element.setAttribute("aria-label", data.summary);
    }

    redrawOnThemeChange(draw, chart);
  }

  /* Income sources → budget → expense categories and savings. Colours follow the role, not the category. */
  function sankeyChart(element) {
    const data = chartData(element);
    if (!data) return;
    const chart = window.echarts.init(element, null, { renderer: "svg" });

    function draw() {
      const t = tokens();
      const roles = { income: t.accent, expenses: t.series2, savings: t.series3 };
      const narrow = element.clientWidth < 620;
      const side = narrow ? 96 : 220;
      const share = (n) => (n.share === null || n.share === undefined ? "" : " · " + pct(n.share, 0));
      /* One line per label, so that small neighbouring nodes keep theirs; a narrow chart shows the names
         only (the amounts are in the tooltip and the table). Labels that would still overlap are hidden. */
      const node = (n, position) => ({
        name: n.id,
        value: n.value,
        itemStyle: { color: roles[n.role] || t.baseline, borderColor: t.surface, borderWidth: 1 },
        label: {
          position,
          color: t.ink,
          width: side - 12,
          overflow: "truncate",
          formatter: () => (narrow ? n.label : n.label + "  {sub|" + compact(n.value) + share(n) + "}"),
          rich: { sub: { color: t.ink2, fontSize: 11 } },
        },
        info: n,
      });
      const budget = data.budget;
      const nodes = [
        ...data.left.map((n) => node(n, "left")),
        { name: budget.id, value: budget.value, itemStyle: { color: t.baseline }, label: { position: "top", color: t.ink2, formatter: () => budget.label }, info: budget },
        ...data.right.map((n) => node(n, "right")),
      ];
      const links = [
        ...data.left.map((n) => ({ source: n.id, target: budget.id, value: n.value, lineStyle: { color: roles[n.role], opacity: 0.32 } })),
        ...data.right.map((n) => ({ source: budget.id, target: n.id, value: n.value, lineStyle: { color: roles[n.role], opacity: 0.32 } })),
      ];
      const info = Object.fromEntries([...data.left, budget, ...data.right].map((n) => [n.id, n]));
      chart.setOption(
        {
          animation: false,
          tooltip: {
            trigger: "item",
            backgroundColor: t.surface,
            borderColor: t.grid,
            textStyle: { color: t.ink },
            extraCssText: "box-shadow: none; border-radius: 8px;",
            formatter(p) {
              const value = escapeHtml(number(p.value, 0) + " " + data.suffix);
              if (p.dataType === "edge") {
                const label = info[p.data.source].label + " → " + info[p.data.target].label;
                return '<div class="tt-value">' + value + '</div><div class="tt-label">' + escapeHtml(label) + "</div>";
              }
              const n = info[p.name];
              const extra = n.share === null || n.share === undefined ? "" : " · " + data.share_text.replace("{share}", pct(n.share, 1));
              return '<div class="tt-value">' + value + '</div><div class="tt-label">' + escapeHtml(n.label + extra) + "</div>";
            },
          },
          series: [
            {
              type: "sankey",
              left: side,
              right: side,
              top: 24,
              bottom: 8,
              nodeAlign: "justify",
              layoutIterations: 0,
              nodeWidth: 12,
              nodeGap: narrow ? 8 : 12,
              draggable: false,
              emphasis: { focus: "adjacency" },
              labelLayout: { hideOverlap: true },
              data: nodes,
              links,
            },
          ],
        },
        true
      );
      element.setAttribute("aria-label", data.summary);
    }

    redrawOnThemeChange(draw, chart);
  }

  function reportCharts() {
    if (typeof window.echarts === "undefined") return;
    document.querySelectorAll('[data-chart="bars"]').forEach(barsChart);
    document.querySelectorAll('[data-chart="sankey"]').forEach(sankeyChart);
  }

  /* ---------------------------------------------------------- the wall monitor */

  /* One chart at a time: the panel is replaced every two minutes, so the old chart is let go and the
     window's listeners (registered once) always reach the current one. */
  let monitorInstance = null;
  let monitorDraw = null;

  function monitorChart(element) {
    if (typeof window.echarts === "undefined") return;
    const data = chartData(element);
    if (!data || !data.dates.length) return;
    if (monitorInstance) monitorInstance.dispose();
    const chart = window.echarts.init(element, null, { renderer: "svg" });
    monitorInstance = chart;
    const label = (v) => (data.percent ? pct(v, 0) : compact(v));
    const last = data.values.length - 1;
    monitorDraw = function draw() {
      const t = tokens();
      chart.setOption(
        {
          animation: false,
          grid: { left: 4, right: 64, top: 14, bottom: 4, containLabel: true },
          tooltip: { show: false }, // a screen on the wall has no pointer: the last value is written out
          xAxis: {
            type: "category",
            boundaryGap: false,
            data: data.dates,
            axisLine: { lineStyle: { color: t.baseline } },
            axisTick: { show: false },
            axisLabel: { color: t.muted, formatter: monthLabel, hideOverlap: true, fontSize: 13 },
          },
          yAxis: {
            type: "value",
            scale: data.percent, // amounts from zero (the area is the amount); a change in percent fits its range
            splitNumber: 4,
            axisLabel: { color: t.muted, formatter: label, fontSize: 13 },
            splitLine: { lineStyle: { color: t.grid, width: 1 } },
          },
          series: [
            {
              type: "line",
              data: data.values.map((v, i) =>
                i === last && v !== null
                  ? { value: v, symbol: "circle", symbolSize: 10, itemStyle: { color: t.accent, borderColor: t.surface, borderWidth: 2 } }
                  : v
              ),
              symbol: "none",
              showSymbol: true,
              connectNulls: false,
              lineStyle: { width: 3, color: t.accent, cap: "round", join: "round" },
              itemStyle: { color: t.accent },
              areaStyle: data.percent ? undefined : { color: t.accent, opacity: 0.1 },
              emphasis: { disabled: true },
              endLabel: {
                show: true,
                color: t.ink2,
                fontSize: 15,
                formatter: (p) => label(typeof p.value === "object" ? p.value.value : p.value),
              },
            },
          ],
        },
        true
      );
    };
    monitorDraw();
  }

  const shortDay = new Intl.DateTimeFormat(textLocale, { weekday: "short", day: "numeric", month: "short" });

  function days(root) {
    root.querySelectorAll("[data-day]").forEach((element) => {
      element.textContent = shortDay.format(parseDay(element.dataset.day));
    });
  }

  function monitor() {
    const clock = document.querySelector("[data-clock]");
    if (!clock) return;
    const day = document.querySelector("[data-date]");
    const time = new Intl.DateTimeFormat(textLocale, { hour: "2-digit", minute: "2-digit" });
    const date = new Intl.DateTimeFormat(textLocale, { weekday: "long", day: "numeric", month: "long" });
    days(document);
    const tick = () => {
      const now = new Date();
      clock.textContent = time.format(now);
      if (day) day.textContent = date.format(now);
    };
    tick();
    window.setInterval(tick, 10000);
    /* keep the screen on while the page is in front, where the browser allows it */
    const awake = () => {
      if (navigator.wakeLock && document.visibilityState === "visible") navigator.wakeLock.request("screen").catch(() => {});
    };
    awake();
    document.addEventListener("visibilitychange", awake);
    document.querySelectorAll('[data-chart="monitor"]').forEach(monitorChart);
    window.addEventListener("resize", () => monitorInstance && monitorInstance.resize());
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => monitorDraw && monitorDraw());
    document.body.addEventListener("htmx:afterSettle", () => {
      days(document);
      document.querySelectorAll('[data-chart="monitor"]').forEach((element) => {
        if (!window.echarts.getInstanceByDom(element)) monitorChart(element);
      });
    });
  }

  /* ---------------------------------------------------------- the menu on a narrow screen */

  function menu() {
    const button = document.querySelector(".menu-button");
    const sidebar = document.querySelector(".sidebar");
    if (!button || !sidebar) return;
    button.addEventListener("click", () => {
      const open = sidebar.classList.toggle("open");
      button.setAttribute("aria-expanded", String(open));
    });
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

  /* A part of a page asked for while this device was removed (or the interface restarted): load the page
     again, which shows why, instead of keeping what was on the screen. */
  document.addEventListener("htmx:responseError", (event) => {
    if (event.detail && event.detail.xhr && event.detail.xhr.status === 401) window.location.reload();
  });

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
    menu();
    networthChart();
    reportCharts();
    monitor();
    dropzones();
  });
})();
