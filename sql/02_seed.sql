-- Pilot tenants. Re-runnable: each insert is guarded.
USE SCHEMA OFFER_PORTAL;

INSERT INTO tenants (tenant_id, name, support_email, from_name)
SELECT 'rcm', 'RCM Coop', NULL, 'RCM Coop' WHERE NOT EXISTS (SELECT 1 FROM tenants WHERE tenant_id = 'rcm');
INSERT INTO tenants (tenant_id, name, support_email, from_name)
SELECT 'dorans', 'Farmers Grain Co of Dorans', NULL, 'Farmers Grain Co of Dorans' WHERE NOT EXISTS (SELECT 1 FROM tenants WHERE tenant_id = 'dorans');
INSERT INTO tenants (tenant_id, name, support_email, from_name)
SELECT 'dumond', 'DuMond Farms', 'grain@dumondfarms.com', 'DuMond Grain' WHERE NOT EXISTS (SELECT 1 FROM tenants WHERE tenant_id = 'dumond');

-- RCM locations (from the AgriCharts feed; Culver/Burtonview = Athens, IL)
INSERT INTO locations (tenant_id, name, state)
SELECT 'rcm', v.n, 'IL' FROM (SELECT 'Culver/Burtonview' n UNION ALL SELECT 'Mechanicsburg' UNION ALL SELECT 'Riverton' UNION ALL SELECT 'RPA') v
WHERE NOT EXISTS (SELECT 1 FROM locations l WHERE l.tenant_id = 'rcm' AND l.name = v.n);
INSERT INTO locations (tenant_id, name, state)
SELECT 'dorans', 'Dorans (Mattoon)', 'IL' WHERE NOT EXISTS (SELECT 1 FROM locations WHERE tenant_id = 'dorans');
INSERT INTO locations (tenant_id, name, state)
SELECT 'dumond', 'Union Springs', 'NY' WHERE NOT EXISTS (SELECT 1 FROM locations WHERE tenant_id = 'dumond');

-- JSA platform admin (sign-in by emailed code). Elevator admins are invited from the console.
INSERT INTO users (tenant_id, email, name, role)
SELECT NULL, 'kpostin@jpsi.com', 'Kolten Postin', 'platform_admin'
WHERE NOT EXISTS (SELECT 1 FROM users WHERE email = 'kpostin@jpsi.com');

-- Scrape sources (public cash-bid pages). Staged as suggestions; admins confirm.
INSERT INTO scrape_sources (tenant_id, kind, url)
SELECT 'rcm', 'agricharts', 'https://www.rcmcoop.com'
WHERE NOT EXISTS (SELECT 1 FROM scrape_sources WHERE tenant_id = 'rcm');
INSERT INTO scrape_sources (tenant_id, kind, url, location_name)
SELECT 'dorans', 'bushelsites', 'https://www.doransfarmers.com/cash-bids', 'Dorans (Mattoon)'
WHERE NOT EXISTS (SELECT 1 FROM scrape_sources WHERE tenant_id = 'dorans');
