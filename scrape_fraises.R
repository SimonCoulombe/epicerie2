# scrape_fraises.R — Daily strawberry price scraper
#
# Tracks fraises (454 g / 1 lb) at Super C, Maxi, Metro, IGA (Quebec)
# Appends one row per store per day to prix_fraises.csv
# Requires FlareSolverr running at FLARESOLVERR_URL (for bot‑protected stores)
#
# Usage:
#   Rscript scrape_fraises.R
# Schedule (Windows Task Scheduler):
#   schtasks /create /tn "ScrapeFraises" \
#     /tr "\"C:\Program Files\R\R-4.4.3\bin\Rscript.exe\" \"C:\Users\Simon\git\epicerie2\scrape_fraises.R\"" \
#     /sc daily /st 08:00

suppressPackageStartupMessages({
  library(httr)
  library(jsonlite)
  library(rvest)
  library(stringr)
})

# ── Configuration ─────────────────────────────────────────────────────────────

FLARESOLVERR_URL <- "http://192.168.2.15:8191/v1"
FLARE_TIMEOUT    <- 60L  # seconds; FlareSolverr will error if it can't solve in time

# Resolve script directory so the CSV is always next to this file
script_dir <- local({
  args     <- commandArgs(trailingOnly = FALSE)
  file_arg <- grep("^--file=", args, value = TRUE)
  if (length(file_arg) > 0) dirname(normalizePath(sub("^--file=", "", file_arg[1])))
  else getwd()
})

CSV_FILE <- file.path(script_dir, "prix_fraises.csv")

STORES <- list(
  list(
    name      = "Super C",
    url       = "https://www.superc.ca/allees/fruits-et-legumes/fruits/baies-et-cerises/fraises/p/665290001184",
    use_flare = TRUE
  ),
  list(
    name      = "Maxi",
    url       = "https://www.maxi.ca/fr/fraises-1-lb/p/20049778001_EA",
    use_flare = TRUE
  ),
  list(
    name      = "Metro",
    url       = "https://www.metro.ca/epicerie-en-ligne/allees/fruits-et-legumes/fruits/baies-et-cerises/fraises/p/665290001184",
    use_flare = TRUE
  ),
  list(
    name      = "IGA",
    url       = "https://www.iga.ca/fr/produits/fraises-454-g",
    use_flare = FALSE
  )
)

# ── HTTP helpers ──────────────────────────────────────────────────────────────

# Fetch page HTML via FlareSolverr (bypasses Cloudflare / bot protection)
flare_get <- function(url) {
  body <- list(
    cmd        = "request.get",
    url        = url,
    maxTimeout = FLARE_TIMEOUT * 1000L
  )
  resp <- POST(
    FLARESOLVERR_URL,
    body    = toJSON(body, auto_unbox = TRUE),
    add_headers("Content-Type" = "application/json"),
    timeout(FLARE_TIMEOUT + 15)
  )
  result <- fromJSON(content(resp, "text", encoding = "UTF-8"))
  if (result$status != "ok") stop("FlareSolverr error: ", result$message)
  result$solution$response
}

# Fetch page HTML directly (no JS needed — SSR pages like IGA)
plain_get <- function(url) {
  resp <- GET(
    url,
    add_headers(
      "User-Agent"      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      "Accept"          = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language" = "fr-CA,fr;q=0.9,en;q=0.5"
    ),
    timeout(30)
  )
  stop_for_status(resp)
  content(resp, "text", encoding = "UTF-8")
}

# ── Price extraction helpers ──────────────────────────────────────────────────

# Parse first numeric price from JSON-LD <script> blocks.
# Handles: { offers: { price: "4.99" } }  and  { offers: [ { price: "4.99" }, ... ] }
price_from_jsonld <- function(html) {
  pg    <- read_html(html)
  nodes <- html_nodes(pg, 'script[type="application/ld+json"]')
  for (n in nodes) {
    j <- tryCatch(fromJSON(html_text(n), simplifyVector = FALSE), error = function(e) NULL)
    if (is.null(j)) next
    offers <- j[["offers"]]
    if (is.null(offers)) next
    # offers may be a list (single) or a list-of-lists (multiple)
    price_str <- if (is.list(offers[[1]])) offers[[1]][["price"]] else offers[["price"]]
    if (!is.null(price_str)) {
      p <- suppressWarnings(as.numeric(price_str))
      if (!is.na(p)) return(p)
    }
  }
  NA_real_
}

# Extract price from a CSS selector (Super C / Metro shared front-end).
# Handles both French "3,99 $" and English "$3.99" formats.
price_from_css <- function(html, selector = ".pi--prices") {
  pg    <- read_html(html)
  nodes <- html_nodes(pg, selector)
  if (length(nodes) == 0) return(NA_real_)
  txt <- html_text(nodes[[1]])
  # French: 3,99 $
  m <- str_match(txt, "([0-9]+),([0-9]{2})\\s*\\$")
  if (!is.na(m[1, 1])) return(as.numeric(paste0(m[1, 2], ".", m[1, 3])))
  # English: $3.99
  m <- str_match(txt, "\\$([0-9]+)\\.([0-9]{2})")
  if (!is.na(m[1, 1])) return(as.numeric(paste0(m[1, 2], ".", m[1, 3])))
  NA_real_
}

# ── Per-store scrapers ────────────────────────────────────────────────────────

scrape_superc <- function(html) {
  p <- price_from_jsonld(html)
  if (is.na(p)) p <- price_from_css(html, ".pi--prices")
  p
}

scrape_maxi <- function(html) {
  price_from_jsonld(html)
}

scrape_metro <- function(html) {
  p <- price_from_jsonld(html)
  if (is.na(p)) p <- price_from_css(html, ".pi--prices")
  p
}

scrape_iga <- function(html) {
  price_from_jsonld(html)
}

SCRAPERS <- list(
  "Super C" = scrape_superc,
  "Maxi"    = scrape_maxi,
  "Metro"   = scrape_metro,
  "IGA"     = scrape_iga
)

# ── Main ──────────────────────────────────────────────────────────────────────

today <- as.character(Sys.Date())
rows  <- list()

for (store in STORES) {
  cat(sprintf("[%s] Scraping %-8s ... ", format(Sys.time(), "%H:%M:%S"), store$name))
  price <- tryCatch({
    html  <- if (store$use_flare) flare_get(store$url) else plain_get(store$url)
    SCRAPERS[[store$name]](html)
  }, error = function(e) {
    cat("ERREUR:", conditionMessage(e), "\n")
    NA_real_
  })
  label <- if (is.na(price)) "prix introuvable" else sprintf("%.2f $", price)
  cat(label, "\n")
  rows[[length(rows) + 1]] <- data.frame(
    date  = today,
    store = store$name,
    price = price,
    url   = store$url,
    stringsAsFactors = FALSE
  )
}

new_data <- do.call(rbind, rows)

# Merge with existing CSV — replace today's rows to avoid duplicates on re-run
if (file.exists(CSV_FILE)) {
  existing <- tryCatch(
    read.csv(CSV_FILE, stringsAsFactors = FALSE),
    error = function(e) { warning("Could not read CSV: ", conditionMessage(e)); NULL }
  )
  if (!is.null(existing)) {
    existing <- existing[existing$date != today, ]
    new_data  <- rbind(existing, new_data)
  }
}

write.csv(new_data, CSV_FILE, row.names = FALSE)
cat(sprintf("\nFichier mis a jour : %s  (%d ligne(s) au total)\n", CSV_FILE, nrow(new_data)))
