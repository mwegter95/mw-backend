import { DatabaseSync } from 'node:sqlite';
import * as bcrypt from 'bcryptjs';
import path from 'path';
import fs from 'fs';

const DATA_DIR = path.join(__dirname, '..', '..', 'data');
if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

const DB_PATH = path.join(DATA_DIR, 'orschell.db');
export const db = new DatabaseSync(DB_PATH);

db.exec('PRAGMA journal_mode = WAL');
db.exec('PRAGMA foreign_keys = ON');

// ── Schema ────────────────────────────────────────────────────────────────────

db.exec(`
CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  name          TEXT NOT NULL DEFAULT '',
  role          TEXT NOT NULL DEFAULT 'customer' CHECK(role IN ('admin','customer')),
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,
  slug          TEXT UNIQUE NOT NULL,
  description   TEXT NOT NULL DEFAULT '',
  display_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS products (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id   INTEGER NOT NULL REFERENCES categories(id),
  name          TEXT NOT NULL,
  slug          TEXT UNIQUE NOT NULL,
  description   TEXT NOT NULL DEFAULT '',
  price         REAL NOT NULL,
  sku           TEXT UNIQUE NOT NULL,
  image_url     TEXT NOT NULL DEFAULT '',
  specs_json    TEXT,
  featured      INTEGER NOT NULL DEFAULT 0,
  active        INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS inventory (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id          INTEGER UNIQUE NOT NULL REFERENCES products(id),
  quantity            INTEGER NOT NULL DEFAULT 0,
  low_stock_threshold INTEGER NOT NULL DEFAULT 5,
  updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cart_items (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL REFERENCES users(id),
  product_id  INTEGER NOT NULL REFERENCES products(id),
  quantity    INTEGER NOT NULL DEFAULT 1,
  UNIQUE(user_id, product_id)
);

CREATE TABLE IF NOT EXISTS orders (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id               INTEGER NOT NULL REFERENCES users(id),
  status                TEXT NOT NULL DEFAULT 'pending',
  subtotal              REAL NOT NULL,
  tax                   REAL NOT NULL,
  total                 REAL NOT NULL,
  shipping_name         TEXT NOT NULL DEFAULT '',
  shipping_address_json TEXT NOT NULL DEFAULT '{}',
  payment_ref           TEXT NOT NULL DEFAULT '',
  created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS order_items (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id    INTEGER NOT NULL REFERENCES orders(id),
  product_id  INTEGER NOT NULL REFERENCES products(id),
  quantity    INTEGER NOT NULL,
  unit_price  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id);
CREATE INDEX IF NOT EXISTS idx_products_featured  ON products(featured);
CREATE INDEX IF NOT EXISTS idx_products_active    ON products(active);
CREATE INDEX IF NOT EXISTS idx_inventory_product  ON inventory(product_id);
CREATE INDEX IF NOT EXISTS idx_orders_user        ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status      ON orders(status);
CREATE INDEX IF NOT EXISTS idx_cart_user          ON cart_items(user_id);
`);

// Category-matched product images (Unsplash, cropped to 400x300)
const PRODUCT_IMAGES: Record<string, string> = {
  'OES-APP-001': 'https://images.unsplash.com/photo-1521572163474-6864f9cf17ab?w=400&h=300&fit=crop&auto=format',
  'OES-APP-002': 'https://images.unsplash.com/photo-1576566588023-fa24843b1cd5?w=400&h=300&fit=crop&auto=format',
  'OES-APP-003': 'https://images.unsplash.com/photo-1583743814966-8936f5b7be1a?w=400&h=300&fit=crop&auto=format',
  'OES-APP-004': 'https://images.unsplash.com/photo-1556821840-3a63f95609a7?w=400&h=300&fit=crop&auto=format',
  'OES-APP-005': 'https://images.unsplash.com/photo-1620799140408-ed84325994bf?w=400&h=300&fit=crop&auto=format',
  'OES-APP-006': 'https://images.unsplash.com/photo-1622445275463-091aa092ee53?w=400&h=300&fit=crop&auto=format',
  'OES-APP-007': 'https://images.unsplash.com/photo-1622445275278-aa1872a4d472?w=400&h=300&fit=crop&auto=format',
  'OES-HAT-001': 'https://images.unsplash.com/photo-1588854337115-641cb87f495d?w=400&h=300&fit=crop&auto=format',
  'OES-HAT-002': 'https://images.unsplash.com/photo-1575428652379-f29a2ecb84ec?w=400&h=300&fit=crop&auto=format',
  'OES-HAT-003': 'https://images.unsplash.com/photo-1576871337628-94c88e106bb6?w=400&h=300&fit=crop&auto=format',
  'OES-HAT-004': 'https://images.unsplash.com/photo-1584196140869-4ad659ba2f69?w=400&h=300&fit=crop&auto=format',
  'OES-SAF-001': 'https://images.unsplash.com/photo-1581094794329-c8112a89af11?w=400&h=300&fit=crop&auto=format',
  'OES-SAF-002': 'https://images.unsplash.com/photo-1504307651254-35680f386031?w=400&h=300&fit=crop&auto=format',
  'OES-SAF-003': 'https://images.unsplash.com/photo-1574258493463-597affd85e3b?w=400&h=300&fit=crop&auto=format',
  'OES-SAF-004': 'https://images.unsplash.com/photo-1606107557195-0dfe29fe3931?w=400&h=300&fit=crop&auto=format',
  'OES-SAF-005': 'https://images.unsplash.com/photo-1544967080-df0861341047?w=400&h=300&fit=crop&auto=format',
  'OES-SIT-001': 'https://images.unsplash.com/photo-1581235720905-c7856a69de87?w=400&h=300&fit=crop&auto=format',
  'OES-SIT-002': 'https://images.unsplash.com/photo-1581574260467-24ac2a95a7d3?w=400&h=300&fit=crop&auto=format',
  'OES-SIT-003': 'https://images.unsplash.com/photo-1513828583688-c42646deb504?w=400&h=300&fit=crop&auto=format',
  'OES-SIT-004': 'https://images.unsplash.com/photo-1594035910387-bea5f4b2b2d4?w=400&h=300&fit=crop&auto=format',
  'OES-SIT-005': 'https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=400&h=300&fit=crop&auto=format',
  'OES-ACC-001': 'https://images.unsplash.com/photo-1615663645117-c27aa2f64d90?w=400&h=300&fit=crop&auto=format',
  'OES-ACC-002': 'https://images.unsplash.com/photo-1579204110769-ab43dead279a?w=400&h=300&fit=crop&auto=format',
  'OES-ACC-003': 'https://images.unsplash.com/photo-1614686846290-a38c4da37827?w=400&h=300&fit=crop&auto=format',
  'OES-ACC-004': 'https://images.unsplash.com/photo-1611157209089-9d8e9eb7d870?w=400&h=300&fit=crop&auto=format',
};

function productImageUrl(sku: string): string {
  return PRODUCT_IMAGES[sku] ?? `https://images.unsplash.com/photo-1504307651254-35680f386031?w=400&h=300&fit=crop&auto=format`;
}

// Refresh product images when seed data used placeholder picsum URLs
const updateImages = db.prepare('UPDATE products SET image_url = ? WHERE sku = ?');
for (const [sku, url] of Object.entries(PRODUCT_IMAGES)) {
  updateImages.run(url, sku);
}

// ── Seed ─────────────────────────────────────────────────────────────────────

const userCount = (db.prepare('SELECT COUNT(*) as c FROM users').get() as any).c;
if (userCount === 0) {
  console.log('[db] Seeding database...');

  // Users
  db.prepare('INSERT INTO users (email,password_hash,name,role) VALUES (?,?,?,?)').run(
    'admin@orschellsupply.com', bcrypt.hashSync('Admin1234!', 10), 'Admin', 'admin'
  );
  const custResult = db.prepare('INSERT INTO users (email,password_hash,name,role) VALUES (?,?,?,?)').run(
    'demo@customer.com', bcrypt.hashSync('Demo1234!', 10), 'Demo Customer', 'customer'
  ) as any;
  const customerId = custResult.lastInsertRowid as number;

  // Categories
  const catIds: Record<string, number> = {};
  const cats: Array<[string, string, string, number]> = [
    ['Apparel',        'apparel',        'Orschell branded apparel and clothing',     1],
    ['Hats & Headwear','hats-headwear',  'Snapbacks, beanies, and headwear',          2],
    ['Safety Gear',    'safety-gear',    'Hi-vis vests, hard hats, gloves, and PPE', 3],
    ['Site Supplies',  'site-supplies',  'Marking paint, tarps, stakes, and more',    4],
    ['Accessories',    'accessories',    'Mousepads, stickers, keychains, and more',  5],
  ];
  for (const [name, slug, desc, order] of cats) {
    const r = db.prepare('INSERT INTO categories (name,slug,description,display_order) VALUES (?,?,?,?)').run(name, slug, desc, order) as any;
    catIds[slug] = r.lastInsertRowid as number;
  }

  // Products: [catSlug, name, sku, description, price, featured]
  const prods: Array<[string, string, string, string, number, boolean]> = [
    ['apparel',       'Dirt Hammers Tee Black',       'OES-APP-001', 'The original Dirt Hammers tee in classic black. Heavy cotton, bold print.',                                    20, true],
    ['apparel',       'Keep Banging Em Tee Black',     'OES-APP-002', 'Keep Banging Em. The Orschell motto on premium black cotton.',                                                 20, false],
    ['apparel',       'YouTube Tee Grey',              'OES-APP-003', 'As seen on the YouTube channel. Soft grey cotton, Orschell logo.',                                             20, false],
    ['apparel',       'Pullover Hoodie Black',         'OES-APP-004', 'Midweight pullover hoodie with Orschell logo. Kangaroo pocket.',                                               38, true],
    ['apparel',       'Zip-Up Hoodie Dark Grey',       'OES-APP-005', 'Full zip hoodie in dark grey. Embroidered chest logo.',                                                        40, false],
    ['apparel',       'Long Sleeve Tee',               'OES-APP-006', 'Lightweight long sleeve in black. Clean minimal design.',                                                      12, false],
    ['apparel',       'Shatter Tri Tee White',         'OES-APP-007', "Limited run shatter graphic tee on white. Collector's item.",                                                   5, false],
    ['hats-headwear', 'Trucker Snapback Orange/Black', 'OES-HAT-001', 'Classic trucker snapback. Orange front, black mesh back. Orschell logo.',                                     20, true],
    ['hats-headwear', 'Flat Bill Snapback Grey',       'OES-HAT-002', 'Flat bill snapback in heather grey. Embroidered logo.',                                                       20, false],
    ['hats-headwear', 'Fleece Beanie Black',           'OES-HAT-003', 'Soft fleece beanie for cold jobsites. One size fits all.',                                                    15, false],
    ['hats-headwear', 'New Era Knit Beanie',           'OES-HAT-004', 'Premium New Era knit beanie with Orschell patch.',                                                            15, false],
    ['safety-gear',   'Hi-Vis Safety Vest Class 2',   'OES-SAF-001', 'ANSI Class 2 high-visibility vest. Adjustable fit. OSHA compliant.',                                          18, true],
    ['safety-gear',   'Hard Hat Yellow',               'OES-SAF-002', 'Type I Class E hard hat in safety yellow. Meets ANSI Z89.1.',                                                 24, false],
    ['safety-gear',   'Safety Glasses Clear',          'OES-SAF-003', 'ANSI Z87.1 rated clear safety glasses. Wraparound lens.',                                                      8, false],
    ['safety-gear',   'Leather Work Gloves',           'OES-SAF-004', 'Full-grain leather work gloves. Superior grip, durable wear.',                                                16, false],
    ['safety-gear',   'Hi-Vis Rain Jacket',            'OES-SAF-005', 'Waterproof hi-vis rain jacket. Class 3 reflective tape. Windproof.',                                          55, false],
    ['site-supplies', 'Marking Paint Orange',          'OES-SIT-001', 'High-visibility orange marking paint. 17oz inverted tip. For dirt or grass.',                                 12, false],
    ['site-supplies', 'Caution Tape 300ft',            'OES-SIT-002', 'Black/yellow caution tape, 300ft roll. 3-mil thickness.',                                                      9, false],
    ['site-supplies', 'Surveying Stakes 50-pack',      'OES-SIT-003', '18" hardwood surveying stakes, pre-pointed. 50-count bundle.',                                                22, false],
    ['site-supplies', 'Heavy Duty Tarp 10x12',         'OES-SIT-004', '6-mil polyethylene tarp, 10x12ft. Grommets every 18 inches. Waterproof.',                                    28, false],
    ['site-supplies', 'Flagging Ribbon',               'OES-SIT-005', '150ft roll of orange flagging ribbon. UV-resistant.',                                                          6, false],
    ['accessories',   'Orschell Mouse Pad',            'OES-ACC-001', 'Oversized desk mouse pad with Orschell branding. 900x400mm.',                                                 10, false],
    ['accessories',   'Sticker Pack',                  'OES-ACC-002', 'Pack of 6 Orschell stickers. Die-cut vinyl, weatherproof.',                                                    5, false],
    ['accessories',   'Decal Set',                     'OES-ACC-003', 'Set of 3 premium vinyl decals for trucks or equipment.',                                                        8, false],
    ['accessories',   'Keychain',                      'OES-ACC-004', 'Orschell metal keychain. Powder-coated finish.',                                                               7, false],
  ];

  const stocks: Record<string, number> = {
    'OES-APP-001':50,'OES-APP-002':45,'OES-APP-003':30,'OES-APP-004':25,'OES-APP-005':20,'OES-APP-006':60,'OES-APP-007':4,
    'OES-HAT-001':40,'OES-HAT-002':35,'OES-HAT-003':50,'OES-HAT-004':45,
    'OES-SAF-001':100,'OES-SAF-002':75,'OES-SAF-003':200,'OES-SAF-004':80,'OES-SAF-005':30,
    'OES-SIT-001':150,'OES-SIT-002':200,'OES-SIT-003':60,'OES-SIT-004':40,'OES-SIT-005':250,
    'OES-ACC-001':30,'OES-ACC-002':100,'OES-ACC-003':75,'OES-ACC-004':60,
  };

  const productIds: Record<string, number> = {};
  for (const [catSlug, name, sku, description, price, featured] of prods) {
    const slug = sku.toLowerCase();
    const imgUrl = productImageUrl(sku);
    const r = db.prepare(
      'INSERT INTO products (category_id,name,slug,description,price,sku,image_url,featured) VALUES (?,?,?,?,?,?,?,?)'
    ).run(catIds[catSlug], name, slug, description, price, sku, imgUrl, featured ? 1 : 0) as any;
    const pid = r.lastInsertRowid as number;
    productIds[sku] = pid;
    db.prepare('INSERT INTO inventory (product_id,quantity,low_stock_threshold) VALUES (?,?,5)').run(pid, stocks[sku] ?? 50);
  }

  // Past orders for demo customer
  const addr = JSON.stringify({ street: '123 Main St', city: 'West Harrison', state: 'IN', zip: '47060' });

  const ord1 = db.prepare(
    'INSERT INTO orders (user_id,status,subtotal,tax,total,shipping_name,shipping_address_json,payment_ref,created_at) VALUES (?,?,?,?,?,?,?,?,?)'
  ).run(customerId, 'delivered', 20.00, 1.40, 21.40, 'Demo Customer', addr, 'MOCK-PAY-001', '2026-05-15 10:00:00') as any;
  db.prepare('INSERT INTO order_items (order_id,product_id,quantity,unit_price) VALUES (?,?,?,?)').run(ord1.lastInsertRowid, productIds['OES-APP-001'], 1, 20.00);

  const ord2 = db.prepare(
    'INSERT INTO orders (user_id,status,subtotal,tax,total,shipping_name,shipping_address_json,payment_ref,created_at) VALUES (?,?,?,?,?,?,?,?,?)'
  ).run(customerId, 'shipped', 40.00, 2.80, 42.80, 'Demo Customer', addr, 'MOCK-PAY-002', '2026-06-01 14:30:00') as any;
  db.prepare('INSERT INTO order_items (order_id,product_id,quantity,unit_price) VALUES (?,?,?,?)').run(ord2.lastInsertRowid, productIds['OES-HAT-001'], 2, 20.00);

  console.log('[db] Seed complete.');
}
