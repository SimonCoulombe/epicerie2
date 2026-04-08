# Plan d'expansion — Produits représentatifs de l'IPC (Aliments)

> **This document is the implementation spec.** An AI coding agent should be able to implement all phases by following this document top to bottom. Each phase has explicit acceptance criteria. Do not skip phases — they build on each other.

## 0. Current System Summary (read this first)

### What exists today

| Component | File(s) | Status |
|-----------|---------|--------|
| **Scraper** | `scraper/main.py`, `scraper/browser.py`, `scraper/parsers.py` | Working. Scrapes 4 targets (fraises × 4 stores). Playwright for Super C/Maxi/Metro, httpx for IGA. |
| **DB** | `scraper/db.py` → `data/epicerie.duckdb` | 6 tables: `products`, `store_chains`, `cities`, `stores`, `scrape_targets`, `prices`. |
| **API** | `api/main.py` | FastAPI on port 8000. Endpoints: `/api/products`, `/api/store-chains`, `/api/cities`, `/api/stores`, `/api/prices`. |
| **Frontend** | `frontend/index.html`, `frontend/app.js`, `frontend/style.css` | Plotly.js dashboard. Single product view with chain/city/date filters. |
| **Config** | `config/targets.yaml` | Declarative: 1 product, 4 stores, 4 targets. |
| **Infra** | `epicerie-api.service`, `epicerie-nginx.conf` | systemd + nginx + SSL + cron (8 AM ET daily). |

### Tech stack

- Python 3.12 venv at `/home/ubuntu/epicerie2/venv`
- Playwright 1.58.0 with Chromium (ARM64)
- DuckDB 1.5.1 (embedded, file at `data/epicerie.duckdb`)
- FastAPI 0.135.3 + uvicorn on port 8000
- nginx with Let's Encrypt SSL, rate limiting
- Live URL: https://epicerie.proutgpt.com

### How to activate the environment and run things

```bash
cd /home/ubuntu/epicerie2
source venv/bin/activate

# Run scraper
python -m scraper.main

# Run API (dev)
uvicorn api.main:app --host 127.0.0.1 --port 8000

# Restart production API after code changes
sudo systemctl restart epicerie-api
```

### DuckDB schema (current)

```sql
products(id INTEGER PK, name VARCHAR, slug VARCHAR UNIQUE)
store_chains(id INTEGER PK, name VARCHAR UNIQUE)
cities(id INTEGER PK, name VARCHAR UNIQUE, slug VARCHAR UNIQUE)
stores(id INTEGER PK, store_chain_id FK, city_id FK, address, postal_code, slug UNIQUE)
scrape_targets(id INTEGER PK, product_id FK, store_id FK, url VARCHAR, use_playwright BOOL, active BOOL, UNIQUE(product_id, store_id))
prices(id INTEGER PK, scrape_target_id FK, date DATE, price DECIMAL(8,2), scraped_at TIMESTAMP, UNIQUE(scrape_target_id, date))
```

### Key implementation details

- `scraper/db.py`: `sync_targets(config)` reads YAML and upserts into DB tables. Uses `ON CONFLICT DO NOTHING` for products/cities/stores (DuckDB FK constraint workaround). For scrape_targets, does explicit SELECT+UPDATE or INSERT.
- `scraper/browser.py`: Uses `domcontentloaded` + selector waiting (not `networkidle`). Stealth: Windows UA, webdriver removal, 1920×1080 viewport. Creates new context per page.
- `scraper/parsers.py`: `price_from_jsonld(html)` and `price_from_css(html, selector)` are generic. 4 thin wrappers: `parse_superc`, `parse_maxi`, `parse_metro`, `parse_iga`. The `PARSERS` dict maps name→function.
- `scraper/main.py`: Loads YAML, calls `sync_targets()`, gets active targets, loops sequentially. IGA uses httpx (no Playwright). Others use Playwright. `_STORE_PARSER_MAP` built from YAML `parser` field.
- `config/targets.yaml`: Each target has `product`, `store`, `url`, `use_playwright`, `parser` keys.

### URL patterns per store (verified working)

| Store | Product page pattern | Example |
|-------|---------------------|---------|
| Super C | `superc.ca/allees/{category}/.../p/{UPC}` | `superc.ca/allees/fruits-et-legumes/fruits/baies-et-cerises/fraises/p/665290001184` |
| Maxi | `maxi.ca/fr/{product-slug}/p/{code}_EA` | `maxi.ca/fr/fraises-1-lb/p/20049778001_EA` |
| Metro | `metro.ca/epicerie-en-ligne/allees/{category}/.../p/{UPC}` | `metro.ca/epicerie-en-ligne/allees/fruits-et-legumes/fruits/.../p/665290001184` |
| IGA | `iga.ca/fr/produits/{brand-product-size}` | `iga.ca/fr/produits/fraises-454-g` |

### Search URL patterns (for URL discovery)

| Store | Search URL |
|-------|-----------|
| Super C | `https://www.superc.ca/search?search-bar={query}` |
| Maxi | `https://www.maxi.ca/fr/search?search-bar={query}` |
| Metro | `https://www.metro.ca/epicerie-en-ligne/recherche?filter={query}` |
| IGA | `https://www.iga.ca/fr/recherche?q={query}` |

---

## 1. Contexte

Le projet suit actuellement **1 produit** (fraises 454g) à travers **4 bannières** (Super C, Maxi, Metro, IGA). L'objectif est d'étendre le suivi à l'ensemble des produits alimentaires représentatifs de l'Indice des prix à la consommation (IPC) de Statistique Canada — environ **200 produits épicerie** × 4 bannières = **~800 cibles de scraping**.

Source : [Produits représentatifs de l'IPC](https://www.statcan.gc.ca/fr/programmes-statistiques/document/2301_D68_V1)

---

## 2. Produits alimentaires IPC — Classification proposée

### 2.1 Produits « épicerie » scrapables (~175)

| Catégorie | Exemples | Nb approx |
|-----------|----------|-----------|
| **Fruits frais** | Pommes, bananes, fraises, bleuets, oranges, raisins, ananas, cantaloup, avocats, poires, mandarines, pamplemousse, citron, framboises | ~15 |
| **Légumes frais** | Tomates, concombre, carottes, brocoli, laitue, oignons, céleri, poivrons, pommes de terre, patates douces, champignons, chou, chou-fleur, asperges, haricots verts, ail, navets, maïs, salade emballée | ~20 |
| **Viandes fraîches** | Bœuf haché, biftecks (3 coupes), rôtis (3 coupes), bœuf à ragoût, porc haché, côtelettes de porc (2), rôtis de porc (3), poulet entier, poitrine de poulet, pilons, haut de cuisses, ailes (surgelées), dinde entière, dinde hachée, gigot d'agneau, saucisse de porc, jambon | ~25 |
| **Charcuteries / transformés** | Tranches de bacon, tranches de jambon cuit, tranches de viande cuite, saucisses fumées, saucisse de salami, pâté de foie | ~6 |
| **Poissons / fruits de mer** | Saumon frais, truite, filets surgelés (aiglefin, morue, sole, tilapia), bâtonnets de poisson, crevettes surgelées (crues + cuites), pétoncles, saumon en conserve, thon en conserve, saumon fumé | ~12 |
| **Produits laitiers** | Lait homo, lait partiellement écrémé, lait au chocolat, lait d'amande, lait d'avoine, beurre, crème sure, crème glacée (2), crème à café, fromages (cheddar, mozzarella, cottage, chèvre, tranches, parmesan, crème), yogourt, lait évaporé | ~20 |
| **Œufs** | Oeufs gros | 1 |
| **Boulangerie / céréales** | Pain blanc, pain boulangerie, pain croustillant, pain plat, pains à hamburger, bagels, muffins au son, beignes, tarte aux fruits, gâteau glacé surgelé, farine (2), pâtes sèches, riz, céréales (chaudes + froides), barres granolas, craquelins, croûtons | ~20 |
| **Conserves / bocaux** | Tomates, maïs, pois, haricots verts, fèves au lard, pois chiches, fruits, jus de tomate, jus de légumes, soupe aux légumes, soupe avec viande, concentré bœuf/poulet | ~12 |
| **Condiments / sauces** | Ketchup, moutarde, mayonnaise, sauce spaghetti, sauce barbecue, sauce piquante, salsa, vinaigrete, confiture, cornichons, olives, hoummous, sel, poivre, herbes séchées | ~15 |
| **Surgelés préparés** | Pizza surgelée, lasagne, repas individuel, pâté à la viande, gaufres, pommes frites, boulettes hamburger, croquettes de poulet, baies surgelées, légumes surgelés | ~10 |
| **Boissons non-alcool** | Jus d'orange, jus de pomme, autres jus, boîtes de jus, boissons gazeuses (3 cat.), eau, eau pétillante | ~8 |
| **Collations** | Croustilles (pomme de terre + tortillas), maïs soufflé, barre de chocolat, gomme, noix, raisins secs | ~6 |
| **Épicerie diverse** | Huile cuisson, huile d'olive, beurre d'arachide, beurre d'amande, miel, sirop d'érable, sucre, cacao, café (3 types), thé (2), mélange à soupe, poudre gélatine, repas mac & cheese, tofu, aliments pour bébés, préparation nourrissons, légumes secs | ~18 |
| **Protéines alternatives** | Burgers à base de plantes, saucisses à base de plantes, protéines en poudre | 3 |

### 2.2 Produits NON scrapables en épicerie (~25)

Ces produits de la catégorie « Aliments » de l'IPC sont exclus car ils proviennent de la **restauration** :

- Aliments achetés au comptoir de mets à emporter — mets chinois, pizza, poulet frit
- Aliments achetés dans un restaurant — déjeuner, dîner, souper
- Repas hamburger acheté dans un établissement de restauration rapide
- Sandwich (2 types) acheté en restauration rapide
- Sandwich déjeuner en restauration rapide
- Tasse de café à emporter
- Bière servie dans les restaurants
- Bouteille de vin servie au restaurant
- Vin maison servi en restaurant
- Whisky canadien servi dans les restaurants

### 2.3 Produits alcools/tabac scrapables séparément (~25)

Bières, vins, spiritueux vendus en magasin (SAQ, dépanneurs). Exclu du scope initial — à traiter dans une phase ultérieure car les URL viennent de SAQ.ca, pas des épiceries.

---

## 3. Modifications au schéma DuckDB

### 3.1 Table `products` — ajouter des colonnes

```sql
ALTER TABLE products ADD COLUMN category VARCHAR;     -- Catégorie IPC (ex: "Fruits frais", "Viandes")
ALTER TABLE products ADD COLUMN cpi_name VARCHAR;      -- Nom officiel StatCan (ex: "Fraises fraîches")
ALTER TABLE products ADD COLUMN unit VARCHAR;           -- Taille/unité (ex: "454g", "1kg", "1L", "dz")
```

**Justification** :
- `category` : permet de filtrer/regrouper dans le dashboard (dropdown par catégorie)
- `cpi_name` : nom exact StatCan pour relier nos données aux publications IPC officielles
- `unit` : taille du produit suivi (important car le même produit existe en plusieurs formats)

### 3.2 Table `scrape_targets` — ajouter des colonnes

```sql
ALTER TABLE scrape_targets ADD COLUMN parser VARCHAR DEFAULT 'auto';  -- Clé du parser à utiliser
ALTER TABLE scrape_targets ADD COLUMN last_success DATE;              -- Dernière date de scrape réussi
ALTER TABLE scrape_targets ADD COLUMN fail_count INTEGER DEFAULT 0;   -- Erreurs consécutives
```

**Justification** :
- `parser` : actuellement dans le YAML seulement — le stocker en DB permet de retirer cette info du YAML
- `last_success` / `fail_count` : monitoring à grande échelle — identifier les URLs brisées sans inspecter les logs

### 3.3 Nouvelles tables (optionnel)

```sql
-- Catégories IPC hiérarchiques (si on veut reproduire l'arbre StatCan)
CREATE TABLE product_categories (
    id INTEGER PRIMARY KEY,
    name VARCHAR NOT NULL UNIQUE,
    parent_id INTEGER REFERENCES product_categories(id)
);
```

**Recommandation** : commencer flat avec la colonne `category` sur `products`. Ajouter la hiérarchie plus tard si nécessaire.

### 3.4 Pas de changements nécessaires pour

- `store_chains` — déjà flexible
- `cities` — déjà flexible
- `stores` — déjà flexible
- `prices` — déjà flexible (lié à `scrape_targets`, qui est lié à `products`)

---

## 4. Parsers — Aucun changement majeur

Les 4 parsers existants (`parse_superc`, `parse_maxi`, `parse_metro`, `parse_iga`) sont **génériques** :
- Ils extraient le prix via JSON-LD (`application/ld+json`) ou CSS (`.pi--prices`)
- La même structure de page est utilisée pour TOUS les produits dans chaque enseigne
- **Les parsers actuels fonctionneront pour n'importe quel produit** — aucune logique spécifique au produit

### 4.1 Améliorations mineures à prévoir

1. **Extraction du nom/taille** : en plus du prix, extraire le nom affiché et le format (ex: "Fraises, 454 g") depuis le JSON-LD pour validation automatique (vérifier qu'on scrape le bon produit)
2. **Gestion des promotions** : certaines pages affichent un prix régulier + prix promo — ajouter `regular_price` et `promo_price` dans la table `prices` (optionnel)
3. **Détection de rupture de stock** : certaines pages n'affichent pas de prix quand le produit est indisponible — distinguer "prix non trouvé" de "produit en rupture"

---

## 5. Structure de configuration

### 5.1 Problème avec le YAML actuel

Le fichier `config/targets.yaml` actuel contient ~50 lignes pour 4 cibles. Avec 800 cibles, il ferait **~10 000 lignes** — ingérable.

### 5.2 Solution proposée : migration vers CSV + DB

**Phase 1** : convertir les cibles en un fichier CSV éditable :

```
config/targets.csv
```

| product_slug | store_slug | url | use_playwright | parser |
|-------------|-----------|-----|----------------|--------|
| fraises-454g | superc-default | https://www.superc.ca/... | true | superc |
| fraises-454g | maxi-default | https://www.maxi.ca/... | true | maxi |
| lait-2pct-2l | superc-default | https://www.superc.ca/... | true | superc |

```
config/products.csv
```

| slug | name | category | cpi_name | unit |
|------|------|----------|----------|------|
| fraises-454g | Fraises 454g | Fruits frais | Fraises fraîches | 454g |
| lait-2pct-2l | Lait 2% 2L | Produits laitiers | Lait partiellement écrémé | 2L |

**Phase 2** : `sync_targets()` lit les CSV au lieu du YAML. Le YAML `targets.yaml` garde uniquement la config des stores/cities (qui changent rarement).

### 5.3 Alternative : tout dans DuckDB

Éliminer complètement les fichiers de config pour les targets — utiliser DuckDB comme source de vérité. Ajouter un petit CLI (`python -m scraper.admin add-product ...`) ou une interface web d'admin.

**Recommandation** : Phase 1 (CSV) est le meilleur compromis — éditable manuellement, versionnable dans git, mais compact.

---

## 6. Stratégie de découverte des URLs

### 6.1 Patterns d'URL par bannière

| Bannière | Pattern d'URL produit | Recherche |
|----------|----------------------|-----------|
| **Super C** | `superc.ca/allees/.../p/{UPC}` | `superc.ca/search?search-bar={query}` |
| **Maxi** | `maxi.ca/fr/{slug}/p/{code}_EA` | `maxi.ca/fr/search?search-bar={query}` |
| **Metro** | `metro.ca/epicerie-en-ligne/allees/.../p/{UPC}` | `metro.ca/epicerie-en-ligne/recherche?filter={query}` |
| **IGA** | `iga.ca/fr/produits/{slug}` | `iga.ca/fr/search?q={query}` |

### 6.2 Approche semi-automatique recommandée

1. **Script de recherche** : créer `scraper/url_finder.py` qui :
   - Prend un nom de produit en entrée
   - Interroge la page de recherche de chaque bannière
   - Extrait les URLs des résultats (lien vers la page produit)
   - Affiche les résultats pour sélection manuelle

2. **Processus par lot** :
   - Parcourir la liste des ~175 produits IPC
   - Pour chaque produit, lancer la recherche sur les 4 bannières
   - Sauvegarder les URLs trouvées dans `config/targets.csv`
   - Validation manuelle (vérifier que l'URL correspond au bon produit/format)

3. **Estimation d'effort** :
   - Avec le script semi-auto : ~2-3 secondes par recherche × 700 combinaisons = ~35 minutes de scraping
   - Validation manuelle : la partie la plus longue — certains produits auront plusieurs résultats (ex: "pommes" → Gala, Granny Smith, McIntosh)

### 6.3 Défis de correspondance produit

| Défi | Exemple | Solution |
|------|---------|----------|
| **Nom générique IPC** | "Pommes" | Choisir la variété la plus commune (ex: Gala, sac 3 lb) |
| **Formats multiples** | "Lait partiellement écrémé" → 1L, 2L, 4L | Standardiser sur le format le plus courant (ex: 2L) |
| **Marques multiples** | "Beurre d'arachide" → Kraft, Skippy, marque maison | Privilégier marque maison (prix de base) ou marque nationale la plus commune |
| **Produit introuvable** | "Pétoncles" chez Super C | Marquer comme non disponible, tracker seulement chez les bannières qui l'offrent |
| **URLs instables** | Les codes UPC changent | Monitorer `fail_count` et relancer la recherche semi-auto |

---

## 7. Scraper — Mise à l'échelle

### 7.1 Performance

Actuellement : 4 cibles × ~5 sec/page = ~20 secondes.
Objectif : 800 cibles × ~5 sec/page = **~67 minutes** séquentiellement.

**Optimisations** :
1. **Parallélisme** : lancer 3-4 onglets Playwright simultanément (un par bannière)
2. **IGA sans Playwright** : IGA fonctionne déjà en httpx pur (~0.5 sec/requête) = ~100 produits IGA en 50 sec
3. **Réutilisation de session** : un seul browser context par bannière au lieu d'un par scrape
4. **Objectif réaliste** : ~10-15 minutes avec parallélisme

### 7.2 Gestion des erreurs à grande échelle

- **Retry** : 2 retries avec backoff exponentiel pour les timeouts
- **Circuit breaker** : si une bannière a 5+ échecs consécutifs, arrêter de la scraper et envoyer une alerte
- **Rapport quotidien** : résumé après chaque run (succès/échecs par bannière)

### 7.3 Rate limiting

- Super C / Metro (même parent Loblaw/Sorbey) : ~2 sec entre requêtes
- Maxi (Loblaw) : ~2 sec entre requêtes
- IGA (Sobeys) : ~1 sec entre requêtes (httpx, plus léger)

---

## 8. Dashboard — Adaptations

### 8.1 Filtre par catégorie

Ajouter un dropdown « Catégorie » dans le dashboard :
- Endpoint : `GET /api/categories` (distinct categories from products)
- Le dropdown filtre la liste des produits affichés

### 8.2 Vue multi-produits

Actuellement le dashboard affiche 1 produit à la fois. Pour 200 produits :
- **Vue résumé** : tableau avec prix moyen par catégorie × bannière
- **Vue détail** : garder le graphique Plotly actuel pour un produit sélectionné
- **Indicateur d'inflation** : variation de prix moyenne sur 30 jours par catégorie

### 8.3 Export des données

- Bouton « Télécharger CSV » pour les données visibles
- Endpoint `/api/export` avec filtres

---

## 9. Plan de déploiement par phases

### Phase 1 — Fondations (maintenant)
- [ ] Modifier le schéma DuckDB (ALTER TABLE)
- [ ] Migrer la config vers CSV (products.csv + targets.csv)
- [ ] Adapter `sync_targets()` pour lire le CSV
- [ ] Ajouter le filtre catégorie dans l'API et le dashboard

### Phase 2 — Pilote (10 produits)
- [ ] Choisir 10 produits représentatifs (1-2 par catégorie majeure)
- [ ] Trouver les URLs manuellement pour les 4 bannières (40 cibles)
- [ ] Valider que le scraping fonctionne pour tous
- [ ] Corriger les parsers si des pages ont un format différent

### Phase 3 — Script de découverte d'URLs
- [ ] Créer `scraper/url_finder.py` (recherche semi-automatique)
- [ ] Tester sur les 10 produits du pilote
- [ ] Raffiner les heuristiques de sélection

### Phase 4 — Expansion complète (~175 produits)
- [ ] Lancer la découverte d'URLs par lot
- [ ] Validation manuelle des correspondances
- [ ] Populer `config/targets.csv` complet
- [ ] Implémenter le parallélisme du scraper

### Phase 5 — Dashboard avancé
- [ ] Vue multi-produits avec tableau résumé
- [ ] Indicateurs d'inflation par catégorie
- [ ] Export CSV
- [ ] Alertes (email/webhook) pour les échecs de scraping

### Phase 6 — Alcools (optionnel)
- [ ] Ajouter SAQ.ca comme source
- [ ] Parser spécifique SAQ
- [ ] Intégrer les ~25 produits alcool/tabac de l'IPC

---

## 10. Risques et mitigations

| Risque | Impact | Mitigation |
|--------|--------|------------|
| Bannière renforce le bot protection | Scraping échoue pour 1 bannière | Rotation d'UA, délais aléatoires, headless=false en dernier recours |
| URLs changent sans préavis | Prix manquants | Monitoring `fail_count`, rescraper les URLs brisées automatiquement |
| Volume de requêtes trop élevé | IP bloquée | Rate limiting strict, proxies résidentiels si nécessaire |
| DuckDB trop lent à grande échelle | API lente | DuckDB gère facilement des millions de lignes — non risqué |
| Produit IPC introuvable dans une bannière | Données incomplètes | Acceptable — marquer comme non disponible, tracker dans les bannières qui l'offrent |

---

## 11. Implementation Guide for AI Agent

**Read this section carefully. It tells you exactly what to do, in order, with acceptance criteria.**

### Phase 1 — Schema + Config Migration

#### Step 1.1: ALTER the DuckDB schema

Edit `scraper/db.py` `_create_tables()` to add new columns. Since the DB already exists, also write a migration function that runs `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` on startup. Add these columns:

```sql
-- products table
ALTER TABLE products ADD COLUMN IF NOT EXISTS category VARCHAR;
ALTER TABLE products ADD COLUMN IF NOT EXISTS cpi_name VARCHAR;
ALTER TABLE products ADD COLUMN IF NOT EXISTS unit VARCHAR;

-- scrape_targets table
ALTER TABLE scrape_targets ADD COLUMN IF NOT EXISTS parser VARCHAR DEFAULT 'auto';
ALTER TABLE scrape_targets ADD COLUMN IF NOT EXISTS last_success DATE;
ALTER TABLE scrape_targets ADD COLUMN IF NOT EXISTS fail_count INTEGER DEFAULT 0;
```

Also update the `CREATE TABLE IF NOT EXISTS products` DDL to include these columns for fresh installs.

**Acceptance**: `python -c "from scraper.db import init_db; init_db()"` succeeds. Querying `DESCRIBE products` shows the new columns. Existing data preserved.

#### Step 1.2: Create `config/products.csv`

Create a CSV file with all ~175 scrapable CPI food products. Columns:

```
slug,name,category,cpi_name,unit
fraises-454g,Fraises 454g,Fruits frais,Fraises fraîches,454g
bananes,Bananes,Fruits frais,Bananes,1kg
lait-2pct-2l,Lait 2% 2L,Produits laitiers,Lait partiellement écrémé,2L
...
```

Rules for slugs: lowercase, ASCII, hyphens, no accents. Example: "Bœuf haché" → `boeuf-hache`.
Rules for `unit`: use the most common retail format (e.g., "2L", "454g", "1kg", "dz" for eggs, "900g" for bread).
Rules for `category`: use one of the categories listed in section 2.1 above.
Rules for `cpi_name`: exact StatCan name from the source URL.

The existing product "fraises-454g" must remain with its current slug.

#### Step 1.3: Create `config/targets.csv`

Start with just the existing 4 targets (fraises × 4 stores). Columns:

```
product_slug,store_slug,url,use_playwright,parser
fraises-454g,superc-default,https://www.superc.ca/allees/fruits-et-legumes/fruits/baies-et-cerises/fraises/p/665290001184,true,superc
fraises-454g,maxi-default,https://www.maxi.ca/fr/fraises-1-lb/p/20049778001_EA,true,maxi
fraises-454g,metro-default,https://www.metro.ca/epicerie-en-ligne/allees/fruits-et-legumes/fruits/baies-et-cerises/fraises/p/665290001184,true,metro
fraises-454g,iga-default,https://www.iga.ca/fr/produits/fraises-454-g,false,iga
```

#### Step 1.4: Update `sync_targets()` to read CSV files

Modify `scraper/db.py` `sync_targets()`:
1. Read `config/products.csv` for products (with new columns: category, cpi_name, unit)
2. Read `config/targets.csv` for scrape targets
3. Keep reading stores/cities from `config/targets.yaml` (they change rarely)
4. The sync must be additive — never delete existing DB rows, only insert new or update URLs

Also update `scraper/main.py` to build the `_STORE_PARSER_MAP` from CSV data instead of YAML targets.

**Acceptance**: Delete and recreate the DB. Run `python -m scraper.main`. All 4 existing targets still scrape correctly. `SELECT category, cpi_name, unit FROM products WHERE slug='fraises-454g'` returns the correct values.

#### Step 1.5: Add category API endpoint + dashboard filter

- Add `GET /api/categories` endpoint in `api/main.py` → returns distinct non-null categories from products
- Add a "Catégorie" dropdown in `frontend/index.html` that filters the product list
- When a category is selected, the product dropdown only shows products in that category

**Acceptance**: Visit https://epicerie.proutgpt.com, see the category dropdown. Selecting a category filters products.

### Phase 2 — Pilot (10 products)

#### Step 2.1: Pick 10 products and find their URLs

Choose exactly these 10 pilot products (1-2 per major category):

| slug | cpi_name | category |
|------|----------|----------|
| fraises-454g | Fraises fraîches | Fruits frais |
| bananes | Bananes | Fruits frais |
| brocoli | Brocoli | Légumes frais |
| boeuf-hache | Bœuf haché | Viandes fraîches |
| poitrine-poulet | Poitrine de poulet | Viandes fraîches |
| lait-2pct-2l | Lait partiellement écrémé | Produits laitiers |
| beurre-454g | Beurre | Produits laitiers |
| oeufs-gros-12 | Oeufs, gros | Œufs |
| pain-blanc-675g | Pain blanc | Boulangerie |
| pates-seches-900g | Pâtes sèches | Boulangerie |

For each product, find the URL at all 4 stores by:
1. Use Playwright to visit the search URL for each store (see search patterns in section 0)
2. Extract the first product page link that matches the product
3. Add the URL to `config/targets.csv`

If a product isn't available at a specific store, skip it (don't add a row to targets.csv).

**Acceptance**: `config/targets.csv` has ~40 rows (10 products × 4 stores, minus any missing). Running `python -m scraper.main` scrapes all of them and stores prices.

### Phase 3 — URL Discovery Script

#### Step 3.1: Create `scraper/url_finder.py`

A CLI tool that:
1. Takes a product name (or reads from `config/products.csv`)
2. For each store, launches a Playwright search
3. Extracts product page URLs from search results
4. Outputs candidates as CSV rows ready to append to `config/targets.csv`

```bash
# Single product
python -m scraper.url_finder "Bœuf haché"

# Batch mode: process all products in products.csv that don't have targets yet
python -m scraper.url_finder --batch
```

#### Step 3.2: Implement scraper parallelism

Modify `scraper/main.py` to scrape multiple targets concurrently:
- Group targets by store chain
- Run 4 async workers (one per chain) with shared Playwright browser
- Add 2-second delay between requests to the same chain
- IGA targets use httpx (no Playwright needed, can run in parallel pool)

Also update `scraper/main.py` to update `last_success`/`fail_count` in `scrape_targets` after each scrape.

**Acceptance**: Running the scraper with 40+ targets completes in under 3 minutes (not 3+ minutes sequentially).

### Phase 4 — Full Expansion

Use `url_finder.py --batch` to discover URLs for all ~175 products. Manually review the output. Populate `config/targets.csv`. Run `python -m scraper.main` and verify.

### Phase 5 — Dashboard Improvements

- Add a summary table view: category × chain price averages
- Add 30-day price change indicator per product
- Add CSV export button
- Make the product dropdown searchable (or use a combobox)

### Important constraints for the implementer

1. **Never break existing functionality.** The fraises-454g scraping must keep working throughout all changes. Run `python -m scraper.main` after every phase to verify.
2. **Don't modify the prices data.** The `prices` table has historical data that must be preserved.
3. **Keep parsers generic.** Do NOT create per-product parsers. The existing JSON-LD + CSS approach works for all products on all 4 stores.
4. **Rate limit scraping.** Minimum 2 seconds between requests to the same domain. Do not parallelize within a single domain.
5. **Use the existing venv.** Run `source venv/bin/activate` before any Python commands. If new packages are needed, `pip install` and update `requirements.txt`.
6. **Restart the service after code changes.** Run `sudo systemctl restart epicerie-api` after modifying API or DB code.
7. **Test the live site.** After frontend changes, verify at https://epicerie.proutgpt.com.
8. **Update the README.md file when done**