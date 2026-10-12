# Data shipped with the package

Public reference data only. Personal data never goes into this folder.

## cpi_tcmb_monthly.csv

Annual and monthly percentage changes of the Turkish consumer price index (CPI,
in Turkish TÜFE), 261 months from 2005-01 to 2026-09.

- **Source:** TurkStat (TÜİK) data as listed on the "Tüketici Fiyatları"
  (consumer prices) page of the Central Bank of the Republic of Türkiye (TCMB):
  https://www.tcmb.gov.tr/wps/wcm/connect/TR/TCMB+TR/Main+Menu/Istatistikler/Enflasyon+Verileri/Tuketici+Fiyatlari
- **Retrieved:** 2026-10-09.
- **Columns:**
  - `month`: YYYY-MM
  - `annual_change_pct`: % change from the same month of the previous year
  - `monthly_change_pct`: % change from the previous month
- **Use:** the index levels are rebuilt by chaining the monthly changes and
  scaled so that the 2025 average is 100 (TurkStat's 2025=100 base). Details
  in [docs/METHOD.md](../../docs/METHOD.md).
- **When it is used:** only offline, or after `finance init` before the CPI
  has been updated. `finance price update` fetches newer data from TCMB or EVDS
  and replaces this copy.
