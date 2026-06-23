-- Catalog analytics
--
-- Reporting queries over the resolution catalog joined to inventory. The
-- engine itself never runs these — this is the analyst's view of the same data:
-- what the catalog is made of, where the revenue concentrates, and where
-- availability is hurting demand.
--
-- Two tables (built by analytics/run.py from data/catalog.csv + inventory.json):
--   catalog  (sku, family, diameter, finish, body, sales_count, unit_price,
--             quantity_on_hand, is_customer_facing, is_proprietary)
--   inventory(sku, qty_on_hand, lead_time_days)   -- lead_time_days NULL = in stock
--
-- Each query below is delimited by a "-- name: <title>" line so the runner can
-- execute and label them independently.


-- name: Catalog composition by family
-- Why: the shape of the catalog at a glance — which families carry the SKU
-- count, and how much of that is the customer-facing scope vs. internal/
-- proprietary rows the engine resolves but never suggests.
WITH by_family AS (
    SELECT
        family,
        COUNT(*)                                          AS skus,
        SUM(is_customer_facing)                           AS customer_facing,
        SUM(is_proprietary)                               AS proprietary
    FROM catalog
    GROUP BY family
)
SELECT
    family,
    skus,
    customer_facing,
    proprietary,
    ROUND(100.0 * skus / SUM(skus) OVER (), 1)            AS pct_of_catalog
FROM by_family
ORDER BY skus DESC
LIMIT 15;


-- name: Sales concentration (Pareto by family)
-- Why: classic 80/20 check. Ranks families by lifetime unit sales and walks the
-- running cumulative share, so you can see how few families make up most of the
-- volume — the ones worth protecting from stockouts and prioritizing for
-- resolver vocabulary.
WITH family_sales AS (
    SELECT family, SUM(sales_count) AS units
    FROM catalog
    GROUP BY family
    HAVING units > 0
)
SELECT
    family,
    units,
    ROUND(100.0 * units / SUM(units) OVER (), 1)                          AS pct_of_sales,
    ROUND(100.0 * SUM(units) OVER (ORDER BY units DESC
                                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                / SUM(units) OVER (), 1)                                  AS cumulative_pct
FROM family_sales
ORDER BY units DESC
LIMIT 15;


-- name: Inventory health by family (customer-facing)
-- Why: availability is the customer-visible promise. For each family, what share
-- is in stock, and how exposed is it to long restock leads (>= 20 business
-- days)? Joins the resolver catalog to the inventory snapshot.
WITH cf AS (
    SELECT c.family,
           i.qty_on_hand,
           i.lead_time_days
    FROM catalog c
    JOIN inventory i ON i.sku = c.sku
    WHERE c.is_customer_facing = 1
)
SELECT
    family,
    COUNT(*)                                                        AS skus,
    ROUND(100.0 * SUM(CASE WHEN qty_on_hand > 0 THEN 1 ELSE 0 END)
                / COUNT(*), 1)                                      AS in_stock_pct,
    SUM(CASE WHEN qty_on_hand = 0 AND lead_time_days >= 20
             THEN 1 ELSE 0 END)                                     AS long_lead_oos
FROM cf
GROUP BY family
HAVING skus >= 20
ORDER BY in_stock_pct ASC
LIMIT 15;


-- name: Price-band distribution (customer-facing)
-- Why: how list price spreads across the sellable catalog. NTILE splits priced
-- SKUs into four quartile bands; the band ranges show where the catalog sits.
WITH priced AS (
    SELECT sku, unit_price,
           NTILE(4) OVER (ORDER BY unit_price) AS band
    FROM catalog
    WHERE is_customer_facing = 1 AND unit_price > 0
)
SELECT
    band,
    COUNT(*)              AS skus,
    ROUND(MIN(unit_price), 2) AS min_price,
    ROUND(AVG(unit_price), 2) AS avg_price,
    ROUND(MAX(unit_price), 2) AS max_price
FROM priced
GROUP BY band
ORDER BY band;


-- name: High-velocity stockouts (action list)
-- Why: the operationally useful one. Within each family, rank SKUs by lifetime
-- sales; surface the top-decile sellers that are currently out of stock — the
-- items whose unavailability costs the most. PERCENT_RANK normalizes velocity
-- within the family so big and small families are comparable.
WITH ranked AS (
    SELECT c.sku, c.family, c.sales_count, i.lead_time_days,
           PERCENT_RANK() OVER (PARTITION BY c.family
                                ORDER BY c.sales_count) AS velocity_rank
    FROM catalog c
    JOIN inventory i ON i.sku = c.sku
    WHERE c.is_customer_facing = 1
      AND i.qty_on_hand = 0
)
SELECT sku, family, sales_count, lead_time_days,
       ROUND(velocity_rank, 2) AS velocity_rank
FROM ranked
WHERE velocity_rank >= 0.90
ORDER BY sales_count DESC
LIMIT 20;
