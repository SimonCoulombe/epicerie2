# epicerie2 — Strawberry Price Tracker

Daily scraper that records the price of strawberries (fraises, 454 g / 1 lb) from four Quebec grocery store websites and appends the results to a CSV file.

## Stores tracked

| Store   | Product URL |
|---------|-------------|
| Super C | https://www.superc.ca/allees/fruits-et-legumes/fruits/baies-et-cerises/fraises/p/665290001184 |
| Maxi    | https://www.maxi.ca/fr/fraises-1-lb/p/20049778001_EA |
| Metro   | https://www.metro.ca/epicerie-en-ligne/allees/fruits-et-legumes/fruits/baies-et-cerises/fraises/p/665290001184 |
| IGA     | https://www.iga.ca/fr/produits/fraises-454-g |

## Files

| File | Description |
|------|-------------|
| `scrape_fraises.R` | Main scraping script — run this daily |
| `prix_fraises.csv` | Output CSV; created on first run, appended on subsequent runs |

## Requirements

### R
- **Version:** R 4.4.3 (`C:\Program Files\R\R-4.4.3\bin\Rscript.exe`)
- **Packages:** `httr`, `jsonlite`, `rvest`, `stringr` (all part of a standard tidyverse-adjacent install)

### FlareSolverr
Super C, Maxi, and Metro are protected by Cloudflare. A [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) instance **must be running** before the script is executed.

- **Default URL:** `http://192.168.2.15:8191/v1`
- To change it, edit the `FLARESOLVERR_URL` constant at the top of `scrape_fraises.R`.

IGA uses Next.js server-side rendering and is fetched directly without FlareSolverr.

## Running the script

```powershell
Rscript "C:\Users\Simon\git\epicerie2\scrape_fraises.R"
```

Re-running on the same day safely overwrites that day's rows (no duplicates).

## CSV format

```
"date","store","price","url"
"2026-04-08","Super C",4.99,"https://..."
"2026-04-08","Maxi",4.99,"https://..."
"2026-04-08","Metro",4.99,"https://..."
"2026-04-08","IGA",4.99,"https://..."
```

## How prices are extracted

| Store   | Method |
|---------|--------|
| Super C | FlareSolverr → CSS `.pi--prices` (French format `3,99 $`) |
| Maxi    | FlareSolverr → JSON-LD `offers.price` |
| Metro   | FlareSolverr → CSS `.pi--prices` (English format `$3.99`) |
| IGA     | Plain HTTP GET → JSON-LD `offers.price` |

Both CSS and JSON-LD paths are tried where possible; JSON-LD takes precedence.

## Scheduling

The task scheduler setup is managed manually. To register a daily task at 08:00:

```powershell
$rscript = "C:\Program Files\R\R-4.4.3\bin\Rscript.exe"
$script  = "C:\Users\Simon\git\epicerie2\scrape_fraises.R"
$action  = New-ScheduledTaskAction -Execute $rscript -Argument "`"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At "08:00"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable
Register-ScheduledTask -TaskName "ScrapeFraises" -Action $action -Trigger $trigger -Settings $settings -Force
```

To remove it: `Unregister-ScheduledTask -TaskName "ScrapeFraises" -Confirm:$false`
